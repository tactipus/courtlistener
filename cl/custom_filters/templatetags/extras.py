import random
import re
import urllib.parse
from datetime import datetime, timezone

import waffle
from django import template
from django.core.exceptions import ValidationError
from django.template import Context
from django.template.context import RequestContext
from django.template.defaultfilters import date as date_filter
from django.utils.formats import date_format
from django.utils.html import format_html
from django.utils.http import urlencode
from django.utils.safestring import SafeString, mark_safe
from elasticsearch_dsl import AttrDict, AttrList

from cl.search.constants import ALERTS_HL_TAG, SEARCH_HL_TAG
from cl.search.models import SEARCH_TYPES, Court, Docket, DocketEntry

register = template.Library()


@register.simple_tag(takes_context=True)
def get_full_host(context, username=None, password=None):
    """Return the current URL with the correct protocol and port.

    No trailing slash.

    :param context: The template context that is passed in.
    :type context: RequestContext
    :param username: A HTTP Basic Auth username to show in the URL
    :type username: str
    :param password: A HTTP Basic Auth password to show in the URL
    :type password: str
    """
    if any([username, password]):
        assert all([username, password]), (
            "If a username is provided, a "
            "password must also be provided and "
            "vice versa."
        )
    r = context.get("request")
    if r is None:
        protocol = "http"
        domain_and_port = "courtlistener.com"
    else:
        protocol = "https" if r.is_secure() else "http"
        domain_and_port = r.get_host()

    return mark_safe(
        "{protocol}://{username}{password}{domain_and_port}".format(
            protocol=protocol,
            username="" if username is None else username,
            password="" if password is None else f":{password}@",
            domain_and_port=domain_and_port,
        )
    )


@register.simple_tag(takes_context=True)
def get_canonical_element(context: Context) -> SafeString:
    href = f"{get_full_host(context)}{context['request'].path}"
    return format_html(
        '<link rel="canonical" href="{}" />',
        href,
    )


@register.simple_tag(takes_context=False)
def granular_date(
    obj, field_name, granularity=None, iso=False, default="Unknown"
):
    """Return the date truncated according to its granularity.

    :param obj: The object to get the value from
    :param field_name: The attribute to be converted to a string.
    :param granularity: The granularity to perform. If None, we assume that
        getattr(obj, 'date_%s_field_name') will work.
    :param iso: Whether to return an iso8601 date or a human readable one.
    :return: A string representation of the date.
    """
    from cl.people_db.models import (
        GRANULARITY_DAY,
        GRANULARITY_MONTH,
        GRANULARITY_YEAR,
    )

    if not isinstance(obj, dict):
        # Convert it to a dict. It's easier to convert this way than from a dict
        # to an object.
        obj = obj.__dict__

    d = obj.get(field_name, None)
    if granularity is None:
        date_parts = field_name.split("_")
        granularity = obj[f"{date_parts[0]}_granularity_{date_parts[1]}"]

    if not d:
        return default
    if iso is False:
        if granularity == GRANULARITY_DAY:
            return date_format(d, format="F j, Y")
        elif granularity == GRANULARITY_MONTH:
            return date_format(d, format="F, Y")
        elif granularity == GRANULARITY_YEAR:
            return date_format(d, format="Y")
    else:
        if granularity == GRANULARITY_DAY:
            return date_format(d, format="Y-m-d")
        elif granularity == GRANULARITY_MONTH:
            return date_format(d, format="Y-m")
        elif granularity == GRANULARITY_YEAR:
            return date_format(d, format="Y")

    raise ValidationError(
        "Fell through date granularity template tag. This could mean that you "
        "have a date without an associated granularity. Did you apply the "
        "validation rules? Is full_clean() getting called in your save() "
        "method?"
    )


@register.filter
def get(mapping, key):
    """Emulates the dictionary get. Useful when keys have spaces or other
    punctuation."""
    return mapping.get(key, "")


@register.simple_tag
def random_int(a: int, b: int) -> int:
    return random.randint(a, b)


@register.filter
def get_es_doc_content(
    mapping: AttrDict | dict, scheduled_alert: bool = False
) -> AttrDict | dict | str:
    """
    Returns the ES document content placed in the "_source" field if the
    document is an AttrDict, or just returns the content if it's not necessary
    to extract from "_source" such as in scheduled alerts where the content is
     a dict.

    :param mapping: The AttrDict or dict instance to extract the content from.
    :param scheduled_alert: A boolean indicating if the content belongs to a
    scheduled alert where the content is already in place.
    :return: The ES document content.
    """

    if scheduled_alert:
        return mapping
    try:
        return mapping["_source"]
    except KeyError:
        return ""


# sourced from: https://stackoverflow.com/questions/2272370/sortable-table-columns-in-django
@register.simple_tag
def url_replace(request, value):
    field = "order_by"
    dict_ = request.GET.copy()
    if field in dict_.keys():
        if dict_[field].startswith("-") and dict_[field].lstrip("-") == value:
            dict_[field] = value  # desc to asc
        elif dict_[field] == value:
            dict_[field] = f"-{value}"
        else:  # order_by for different column
            dict_[field] = value
    else:  # No order_by
        dict_[field] = value
    return urlencode(sorted(dict_.items()))


@register.simple_tag
def sort_caret(request, value) -> SafeString:
    current = request.GET.get("order_by", "*UP*")
    caret = '&nbsp;<i class="gray fa fa-angle-up"></i>'
    if current == value or current == f"-{value}":
        if current.startswith("-"):
            caret = '&nbsp;<i class="gray fa fa-angle-down"></i>'
    return mark_safe(caret)


@register.simple_tag
def citation(obj) -> SafeString:
    if isinstance(obj, Docket):
        # Dockets do not have dates associated with them.  This is more
        # of a "weak citation".  It is there to allow people to find the
        # docket
        docket = obj
        date_of_interest = None
        ecf = ""
    elif isinstance(obj, DocketEntry):
        docket = obj.docket
        date_of_interest = obj.date_filed
        ecf = obj.entry_number
    else:
        raise NotImplementedError(f"Object not recongized in {__name__}")

    # We want to build a citation that follows the Bluebook format as much
    # as possible.  For documents from a case that looks like:
    #   name_bb, case_bb, (court_bb date_bb) ECF No. {ecf}"
    # If this is a citation to just a docket then we leave off the ECF number
    # For opinions there is no need as the title of the block IS the citation
    if date_of_interest:
        date_of_interest = date_of_interest.strftime("%b %d, %Y")
    result = f"{docket.case_name}, {docket.docket_number}, ("
    result = result + docket.court.citation_string
    if date_of_interest:
        result = f"{result} {date_of_interest}"
    result = f"{result})"
    if ecf:
        result = f"{result} ECF No. {ecf}"
    return result


@register.simple_tag
def contains_highlights(content: str, alert: bool = False) -> bool:
    """Check if a given string contains the mark tag used in highlights.

    :param content: The input string to check.
    :param alert: Whether this tag is being used in the alert template.
    :return: True if the mark highlight tag is found, otherwise False.
    """
    hl_tag = ALERTS_HL_TAG if alert else SEARCH_HL_TAG
    pattern = rf"<{hl_tag}>.*?</{hl_tag}>"
    matches = re.findall(pattern, content)
    return bool(matches)


@register.filter
def render_string_or_list(value: any) -> any:
    """Filter to render list of strings separated by commas or the original
    value.

    :param value: The value to be rendered.
    :return: The original value or comma-separated values.
    """
    if isinstance(value, (list, AttrList)):
        return ", ".join(str(item) for item in value)
    return value


@register.filter
def get_highlight(result: AttrDict | dict[str, any], field: str) -> any:
    """Returns the highlighted version of the field is present, otherwise,
    falls back to the original field value.

    :param result: The search result object.
    :param field: The name of the field for which to retrieve the highlighted
    version.
    :return: The highlighted field value if available, otherwise, the original
    field value.
    """

    hl_value = None
    original_value = getattr(result, field, "")
    if isinstance(result, AttrDict) and hasattr(result.meta, "highlight"):
        hl_value = getattr(result.meta.highlight, field, None)
    elif isinstance(result, dict):
        hl_value = result.get("meta", {}).get("highlight", {}).get(field)
        original_value = result.get(field, "")

    return render_string_or_list(hl_value) if hl_value else original_value


@register.simple_tag
def extract_q_value(query: str) -> str:
    """Extract the value of the "q" parameter from a URL-encoded query string.

    :param query: The URL-encoded query string.
    :return: The value of the "q" parameter or an empty string if "q" is not found.
    """

    parsed_query = urllib.parse.parse_qs(query)
    return parsed_query.get("q", [""])[0]


@register.simple_tag(takes_context=True)
def alerts_supported(context: RequestContext, search_type: str) -> str:
    """Determine if search alerts are supported based on the search type and flag
    status.

    :param context: The template context, which includes the request, required
    for the waffle flag.
    :param search_type: The type of search being performed.
    :return: True if alerts are supported, False otherwise.
    """

    request = context["request"]
    if search_type == SEARCH_TYPES.RECAP:
        return waffle.flag_is_active(request, "recap-alerts-active")
    return search_type in (SEARCH_TYPES.OPINION, SEARCH_TYPES.ORAL_ARGUMENT)


@register.filter
def group_courts(courts: list[Court], num_columns: int) -> list:
    """Divide courts in equal groupings while keeping related courts together

    :param courts: Courts to group.
    :param num_columns: Number of groups wanted
    :return: The courts grouped together
    """

    column_len = len(courts) // num_columns
    remainder = len(courts) % num_columns

    groups = []
    start = 0
    for index in range(num_columns):
        # Calculate the end index for this chunk
        end = start + column_len + (1 if index < remainder else 0)

        # Find the next COLR as a starting point (Court of last resort)
        COLRs = [Court.TERRITORY_SUPREME, Court.STATE_SUPREME]
        while end < len(courts) and courts[end].jurisdiction not in COLRs:
            end += 1

        # Create the column and add it to result
        groups.append(courts[start:end])
        start = end

    return groups


@register.filter
def format_date(date_str: str) -> str:
    """Formats a date string in the format 'F jS, Y'. Useful for formatting
    ES child document results where dates are not date objects."""
    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        return date_filter(date_obj, "F jS, Y")
    except (ValueError, TypeError):
        return date_str


@register.filter
def datetime_in_utc(date_obj) -> str:
    """Formats a datetime object in UTC with timezone displayed.
    For example: 'Nov. 25, 2024, 01:28 p.m. UTC'"""
    if date_obj is None:
        return ""
    try:
        return date_filter(
            date_obj.astimezone(timezone.utc),
            "M. j, Y, h:i a T",
        )
    except (ValueError, TypeError):
        return date_obj


@register.filter
def build_docket_id_q_param(request_q: str, docket_id: str) -> str:
    """Build a query string that includes the docket ID and any existing query
    parameters.

    :param request_q: The current query string, if present.
    :param docket_id: The docket_id to append to the query string.
    :return:The query string with the docket_id included.
    """

    if request_q:
        return f"({request_q}) AND docket_id:{docket_id}"
    return f"docket_id:{docket_id}"
