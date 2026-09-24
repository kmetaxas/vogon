import json

import markdown
import nh3
from django.template import Library
from django.utils.html import conditional_escape
from django.utils.safestring import mark_safe
from markdown.extensions.codehilite import CodeHiliteExtension

register = Library()

_ALLOWED_TAGS = {
    "p",
    "br",
    "strong",
    "em",
    "a",
    "ul",
    "ol",
    "li",
    "code",
    "pre",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "blockquote",
    "hr",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "span",
    "div",
}

_ALLOWED_ATTRIBUTES = {
    "a": {"href", "title"},
    "code": {"class"},
    "pre": {"class"},
    "span": {"class"},
    "div": {"class"},
}


@register.filter(name="markdownify", is_safe=True)
def markdownify(text: str):
    """Render Markdown text to safe HTML with Pygments syntax highlighting."""
    if not text:
        return ""
    html = markdown.markdown(
        text,
        extensions=[
            "fenced_code",
            "tables",
            "nl2br",
            CodeHiliteExtension(),
        ],
    )
    safe_html = nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        strip_comments=True,
    )
    return mark_safe(safe_html)


@register.filter(name="json_pp")
def json_pp(value) -> str:
    """Pretty-print a JSON-serializable value as indented JSON."""
    if not value:
        return ""
    try:
        return json.dumps(value, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(conditional_escape(str(value)))
