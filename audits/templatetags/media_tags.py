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
