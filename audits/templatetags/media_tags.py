from django import template

register = template.Library()


@register.filter
def thumb_src(field_file) -> str:
    """Small preview URL for an image field: the stored JPEG thumbnail on the
    review instance (~20 KB vs ~2.5 MB PNG); the normal URL elsewhere."""
    if not field_file:
        return ""
    thumb_url = getattr(field_file.storage, "thumb_url", None)
    return thumb_url(field_file.name) if thumb_url else field_file.url


@register.filter
def date_then_time(stamp: str):
    """"Oct 29, 06:42:34 PM EDT" → date and time on two lines, so the column stays narrow."""
    from django.utils.html import conditional_escape
    from django.utils.safestring import mark_safe

    date, sep, rest = str(stamp or "").partition(", ")
    if not sep:
        return stamp
    return mark_safe(f'<span class="review-date">{conditional_escape(date)}</span> {conditional_escape(rest)}')
