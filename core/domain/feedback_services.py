# coding: utf-8
#
# Copyright 2016 The Oppia Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Commands for feedback thread and message operations."""

from __future__ import annotations

from core import feconf, utils
from core.domain import (
    email_manager,
    feedback_domain,
    rights_manager,
    subscription_services,
    taskqueue_services,
    user_services,
)
from core.platform import models

from typing import Dict, Final, List, Optional, Tuple, Type

MYPY = False
if MYPY:  # pragma: no cover
    from mypy_imports import (
        base_models,
        datastore_services,
        exp_models,
        feedback_models,
        question_models,
        skill_models,
        suggestion_models,
        topic_models,
        transaction_services,
    )

(
    base_models,
    exp_models,
    feedback_models,
    question_models,
    skill_models,
    suggestion_models,
    topic_models,
) = models.Registry.import_models(
    [
        models.Names.BASE_MODEL,
        models.Names.EXPLORATION,
        models.Names.FEEDBACK,
        models.Names.QUESTION,
        models.Names.SKILL,
        models.Names.SUGGESTION,
        models.Names.TOPIC,
    ]
)

datastore_services = models.Registry.import_datastore_services()
transaction_services = models.Registry.import_transaction_services()

DEFAULT_SUGGESTION_THREAD_SUBJECT: Final = 'Suggestion from a learner'
DEFAULT_SUGGESTION_THREAD_INITIAL_MESSAGE: Final = ''

TARGET_TYPE_TO_TARGET_MODEL: Dict[str, Type[base_models.BaseModel]] = {
    feconf.ENTITY_TYPE_EXPLORATION: exp_models.ExplorationModel,
    feconf.ENTITY_TYPE_QUESTION: question_models.QuestionModel,
    feconf.ENTITY_TYPE_SKILL: skill_models.SkillModel,
    feconf.ENTITY_TYPE_TOPIC: topic_models.TopicModel,
}


def get_exp_id_from_thread_id(thread_id: str) -> str:
    """Returns the exploration_id part of the thread_id.

    TODO(#8370): Once feedback threads are generalized, this function needs to
    be updated to get the id from any general entity, not just explorations. At
    the moment, it still assumes that the thread id is associated to an
    exploration.

    Args:
        thread_id: str. The id of the thread.

    Returns:
        str. The exploration id part of the thread_id.
    """
    return thread_id.split('.')[1]


def _create_models_for_thread_and_first_message(
    entity_type: str,
    entity_id: str,
    original_author_id: Optional[str],
    subject: str,
    text: str,
    has_suggestion: bool,
) -> str:
    """Creates a feedback thread and its first message.

    Args:
        entity_type: str. The type of entity the feedback thread is linked to.
        entity_id: str. The id of the entity.
        original_author_id: str|None. The author id who starts this thread, or
            None if the author is anonymous.
        subject: str. The subject of this thread.
        text: str. The text of the feedback message. This may be ''.
        has_suggestion: bool. Whether this thread has a related learner
            suggestion.

    Returns:
        str. The id of the new thread.
    """

    @transaction_services.run_in_transaction_wrapper
    def _create_thread_and_message_txn() -> Tuple[
        str, feedback_domain.FeedbackMessage, bool
    ]:
        thread_id = (
            feedback_models.GeneralFeedbackThreadModel.generate_new_thread_id(
                entity_type, entity_id
            )
        )
        # Get the thread from datastore
        thread_model = feedback_models.GeneralFeedbackThreadModel.get_by_id(
            thread_id
        )
        if thread_model is None:
            # Thread doesn't exist yet - create it
            thread_model = feedback_models.GeneralFeedbackThreadModel.create(
                thread_id
            )
            thread_model.entity_type = entity_type
            thread_model.entity_id = entity_id
            thread_model.original_author_id = original_author_id
            # The feedback analytics jobs rely on the thread status being set to 'open'
            # when a new thread is created. If this is changed, changes need to be made
            # there as well.
            thread_model.status = feedback_models.STATUS_CHOICES_OPEN
            thread_model.subject = subject
            thread_model.has_suggestion = has_suggestion
            thread_model.message_count = 0
            thread_model.update_timestamps()
            thread_model.put()
            needs_message_creation = True
        elif thread_model.message_count > 0:
            # Thread already has a message - nothing to do
            # We still need to return the thread_id and the first message
            message_model = feedback_models.GeneralFeedbackMessageModel.get_by_id(
                thread_id, 0
            )
            message = _get_message_from_model(message_model)
            return thread_id, message, False
        # If we get here, thread exists but message_count == 0
        # So we need to create the message and update the thread

        # Create the message
        message = create_message(
            thread_id,
            original_author_id,
            feedback_models.STATUS_CHOICES_OPEN,
            subject,
            text,
            should_send_email=False,
        )
        # The create_message function already updated the thread model
        # (message_count, last_nonempty_message_text, etc.) and put it
        # So we don't need to do additional thread updates here
        needs_message_creation = True

        return thread_id, message, needs_message_creation

    # Run the transaction
    thread_id, message, needs_message_creation = _create_thread_and_message_txn()

    # After transaction succeeds, handle email tasks if needed
    if needs_message_creation and (
        feconf.CAN_SEND_TRANSACTIONAL_EMAILS
        and original_author_id is not None
        and user_services.is_user_registered(original_author_id)
        and len(text) > 0  # For new thread, message_changed iff text non-empty
    ):
        # For a new thread, status doesn't change when adding first message
        old_status = feedback_models.STATUS_CHOICES_OPEN
        new_status = feedback_models.STATUS_CHOICES_OPEN
        _add_message_to_email_buffer(
            original_author_id,
            thread_id,
            message.message_id,
            len(text),
            old_status,
            new_status,
        )

    return thread_id


def create_thread(
    entity_type: str,
    entity_id: str,
    original_author_id: Optional[str],
    subject: str,
    text: str,
    has_suggestion: bool = False,
) -> str:
    """Creates a feedback thread and its first message.

    Args:
        entity_type: str. The type of entity the feedback thread is linked to.
        entity_id: str. The id of the entity.
        original_author_id: str|None. The author id who starts this thread, or
            None if the author is anonymous.
        subject: str. The subject of this thread.
        text: str. The text of the feedback message. This may be ''.
        has_suggestion: bool. Whether this thread has a related learner
            suggestion.

    Returns:
        str. The id of the new thread.
    """
    return _create_models_for_thread_and_first_message(
        entity_type, entity_id, original_author_id, subject, text, has_suggestion
    )


def _increment_open_threads_count_transactional(exp_id: str) -> None:
    """Increments count of open threads by one."""
    model = feedback_models.FeedbackAnalyticsModel.get(
        exp_id, strict=False
    ) or feedback_models.FeedbackAnalyticsModel(id=exp_id, num_open_threads=0)
    model.num_open_threads = (model.num_open_threads or 0) + 1
    model.update_timestamps()
    model.put()


def _increment_total_threads_count_transactional(exp_id: str) -> None:
    """Increments count of total threads by one."""
    model = feedback_models.FeedbackAnalyticsModel.get(
        exp_id, strict=False
    ) or feedback_models.FeedbackAnalyticsModel(id=exp_id, num_total_threads=0)
    model.num_total_threads = (model.num_total_threads or 0) + 1
    model.update_timestamps()
    model.put()


def _decrement_open_threads_count_transactional(exp_id: str) -> None:
    """Decrements count of open threads by one."""
    model = feedback_models.FeedbackAnalyticsModel.get(
        exp_id, strict=False
    ) or feedback_models.FeedbackAnalyticsModel(id=exp_id, num_open_threads=0)
    model.num_open_threads = (model.num_open_threads or 0) - 1
    model.update_timestamps()
    model.put()