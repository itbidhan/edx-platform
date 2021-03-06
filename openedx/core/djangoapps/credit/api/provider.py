"""
API for initiating and tracking requests for credit from a provider.
"""

import logging
import uuid
import datetime

import pytz

from django.db import transaction

from util.date_utils import to_timestamp

from student.models import User
from openedx.core.djangoapps.credit.exceptions import (
    UserIsNotEligible,
    CreditProviderNotConfigured,
    RequestAlreadyCompleted,
    CreditRequestNotFound,
    InvalidCreditStatus,
)
from openedx.core.djangoapps.credit.models import (
    CreditProvider,
    CreditRequirementStatus,
    CreditRequest,
    CreditEligibility,
)
from openedx.core.djangoapps.credit.signature import signature, get_shared_secret_key


log = logging.getLogger(__name__)


def get_credit_providers():
    """
    Retrieve all available credit providers.

    Example:
    >>> get_credit_providers()
    [
        {
            "id": "hogwarts",
            "display_name": "Hogwarts School of Witchcraft and Wizardry"
        },
        ...
    ]

    Returns: list
    """
    return CreditProvider.get_credit_providers()


@transaction.commit_on_success
def create_credit_request(course_key, provider_id, username):
    """
    Initiate a request for credit from a credit provider.

    This will return the parameters that the user's browser will need to POST
    to the credit provider.  It does NOT calculate the signature.

    Only users who are eligible for credit (have satisfied all credit requirements) are allowed to make requests.

    A provider can be configured either with *integration enabled* or not.
    If automatic integration is disabled, this method will simply return
    a URL to the credit provider and method set to "GET", so the student can
    visit the URL and request credit directly.  No database record will be created
    to track these requests.

    If automatic integration *is* enabled, then this will also return the parameters
    that the user's browser will need to POST to the credit provider.
    These parameters will be digitally signed using a secret key shared with the credit provider.

    A database record will be created to track the request with a 32-character UUID.
    The returned dictionary can be used by the user's browser to send a POST request to the credit provider.

    If a pending request already exists, this function should return a request description with the same UUID.
    (Other parameters, such as the user's full name may be different than the original request).

    If a completed request (either accepted or rejected) already exists, this function will
    raise an exception.  Users are not allowed to make additional requests once a request
    has been completed.

    Arguments:
        course_key (CourseKey): The identifier for the course.
        provider_id (str): The identifier of the credit provider.
        user (User): The user initiating the request.

    Returns: dict

    Raises:
        UserIsNotEligible: The user has not satisfied eligibility requirements for credit.
        CreditProviderNotConfigured: The credit provider has not been configured for this course.
        RequestAlreadyCompleted: The user has already submitted a request and received a response
            from the credit provider.

    Example Usage:
        >>> create_credit_request(course.id, "hogwarts", "ron")
        {
            "url": "https://credit.example.com/request",
            "method": "POST",
            "parameters": {
                "request_uuid": "557168d0f7664fe59097106c67c3f847",
                "timestamp": 1434631630,
                "course_org": "HogwartsX",
                "course_num": "Potions101",
                "course_run": "1T2015",
                "final_grade": 0.95,
                "user_username": "ron",
                "user_email": "ron@example.com",
                "user_full_name": "Ron Weasley",
                "user_mailing_address": "",
                "user_country": "US",
                "signature": "cRCNjkE4IzY+erIjRwOQCpRILgOvXx4q2qvx141BCqI="
            }
        }

    """
    try:
        user_eligibility = CreditEligibility.objects.select_related('course').get(
            username=username,
            course__course_key=course_key
        )
        credit_course = user_eligibility.course
        credit_provider = CreditProvider.objects.get(provider_id=provider_id)
    except CreditEligibility.DoesNotExist:
        log.warning(
            u'User "%s" tried to initiate a request for credit in course "%s", '
            u'but the user is not eligible for credit',
            username, course_key
        )
        raise UserIsNotEligible
    except CreditProvider.DoesNotExist:
        log.error(u'Credit provider with ID "%s" has not been configured.', provider_id)
        raise CreditProviderNotConfigured

    # Check if we've enabled automatic integration with the credit
    # provider.  If not, we'll show the user a link to a URL
    # where the user can request credit directly from the provider.
    # Note that we do NOT track these requests in our database,
    # since the state would always be "pending" (we never hear back).
    if not credit_provider.enable_integration:
        return {
            "url": credit_provider.provider_url,
            "method": "GET",
            "parameters": {}
        }
    else:
        # If automatic credit integration is enabled, then try
        # to retrieve the shared signature *before* creating the request.
        # That way, if there's a misconfiguration, we won't have requests
        # in our system that we know weren't sent to the provider.
        shared_secret_key = get_shared_secret_key(credit_provider.provider_id)
        if shared_secret_key is None:
            msg = u'Credit provider with ID "{provider_id}" does not have a secret key configured.'.format(
                provider_id=credit_provider.provider_id
            )
            log.error(msg)
            raise CreditProviderNotConfigured(msg)

    # Initiate a new request if one has not already been created
    credit_request, created = CreditRequest.objects.get_or_create(
        course=credit_course,
        provider=credit_provider,
        username=username,
    )

    # Check whether we've already gotten a response for a request,
    # If so, we're not allowed to issue any further requests.
    # Skip checking the status if we know that we just created this record.
    if not created and credit_request.status != "pending":
        log.warning(
            (
                u'Cannot initiate credit request because the request with UUID "%s" '
                u'exists with status "%s"'
            ), credit_request.uuid, credit_request.status
        )
        raise RequestAlreadyCompleted

    if created:
        credit_request.uuid = uuid.uuid4().hex

    # Retrieve user account and profile info
    user = User.objects.select_related('profile').get(username=username)

    # Retrieve the final grade from the eligibility table
    try:
        final_grade = CreditRequirementStatus.objects.get(
            username=username,
            requirement__namespace="grade",
            requirement__name="grade",
            status="satisfied"
        ).reason["final_grade"]
    except (CreditRequirementStatus.DoesNotExist, TypeError, KeyError):
        log.exception(
            "Could not retrieve final grade from the credit eligibility table "
            "for user %s in course %s.",
            user.id, course_key
        )
        raise UserIsNotEligible

    parameters = {
        "request_uuid": credit_request.uuid,
        "timestamp": to_timestamp(datetime.datetime.now(pytz.UTC)),
        "course_org": course_key.org,
        "course_num": course_key.course,
        "course_run": course_key.run,
        "final_grade": final_grade,
        "user_username": user.username,
        "user_email": user.email,
        "user_full_name": user.profile.name,
        "user_mailing_address": (
            user.profile.mailing_address
            if user.profile.mailing_address is not None
            else ""
        ),
        "user_country": (
            user.profile.country.code
            if user.profile.country.code is not None
            else ""
        ),
    }

    credit_request.parameters = parameters
    credit_request.save()

    if created:
        log.info(u'Created new request for credit with UUID "%s"', credit_request.uuid)
    else:
        log.info(
            u'Updated request for credit with UUID "%s" so the user can re-issue the request',
            credit_request.uuid
        )

    # Sign the parameters using a secret key we share with the credit provider.
    parameters["signature"] = signature(parameters, shared_secret_key)

    return {
        "url": credit_provider.provider_url,
        "method": "POST",
        "parameters": parameters
    }


def update_credit_request_status(request_uuid, provider_id, status):
    """
    Update the status of a credit request.

    Approve or reject a request for a student to receive credit in a course
    from a particular credit provider.

    This function does NOT check that the status update is authorized.
    The caller needs to handle authentication and authorization (checking the signature
    of the message received from the credit provider)

    The function is idempotent; if the request has already been updated to the status,
    the function does nothing.

    Arguments:
        request_uuid (str): The unique identifier for the credit request.
        provider_id (str): Identifier for the credit provider.
        status (str): Either "approved" or "rejected"

    Returns: None

    Raises:
        CreditRequestNotFound: No request exists that is associated with the given provider.
        InvalidCreditStatus: The status is not either "approved" or "rejected".

    """
    if status not in [CreditRequest.REQUEST_STATUS_APPROVED, CreditRequest.REQUEST_STATUS_REJECTED]:
        raise InvalidCreditStatus

    try:
        request = CreditRequest.objects.get(uuid=request_uuid, provider__provider_id=provider_id)
        old_status = request.status
        request.status = status
        request.save()

        log.info(
            u'Updated request with UUID "%s" from status "%s" to "%s" for provider with ID "%s".',
            request_uuid, old_status, status, provider_id
        )
    except CreditRequest.DoesNotExist:
        msg = (
            u'Credit provider with ID "{provider_id}" attempted to '
            u'update request with UUID "{request_uuid}", but no request '
            u'with this UUID is associated with the provider.'
        ).format(provider_id=provider_id, request_uuid=request_uuid)
        log.warning(msg)
        raise CreditRequestNotFound(msg)


def get_credit_requests_for_user(username):
    """
    Retrieve the status of a credit request.

    Returns either "pending", "approved", or "rejected"

    Arguments:
        username (unicode): The username of the user who initiated the requests.

    Returns: list

    Example Usage:
    >>> get_credit_request_status_for_user("bob")
    [
        {
            "uuid": "557168d0f7664fe59097106c67c3f847",
            "timestamp": 1434631630,
            "course_key": "course-v1:HogwartsX+Potions101+1T2015",
            "provider": {
                "id": "HogwartsX",
                "display_name": "Hogwarts School of Witchcraft and Wizardry",
            },
            "status": "pending"  # or "approved" or "rejected"
        }
    ]

    """
    return CreditRequest.credit_requests_for_user(username)


def get_credit_request_status(username, course_key):
    """Get the credit request status.

    This function returns the status of credit request of user for given course.
    It returns the latest request status for the any credit provider.
    The valid status are 'pending', 'approved' or 'rejected'.

    Args:
        username(str): The username of user
        course_key(CourseKey): The course locator key

    Returns:
        A dictionary of credit request user has made if any

    """
    credit_request = CreditRequest.get_user_request_status(username, course_key)
    return {
        "uuid": credit_request.uuid,
        "timestamp": credit_request.modified,
        "course_key": credit_request.course.course_key,
        "provider": {
            "id": credit_request.provider.provider_id,
            "display_name": credit_request.provider.display_name
        },
        "status": credit_request.status
    } if credit_request else {}
