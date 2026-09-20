"""Theme-matched HTML shell for outgoing email (digest, request-access).

Mirrors the design tokens in app/static/css/app.css -- same dark ground, same
muted accent -- so the morning digest reads as the same product as the app it
came from instead of a bare plain-text dump. Every color is inlined because
email clients strip or ignore <style> blocks inconsistently; the <style> block
that *is* there only carries the dark color-scheme hint Gmail/Apple Mail honor.

Senders keep building their plain-text body exactly as before and pass a
structured list of sections here; the text body still goes out as the
`text` fallback for clients that can't render HTML.
"""
from html import escape

# Keep in sync with :root in app/static/css/app.css.
BG = "#161826"
BG_ELEVATED = "#232532"
BORDER = "#2f313e"
TEXT = "#e9e9ed"
TEXT_MUTED = "#9fa0a7"
TEXT_FAINT = "#75768a"
ACCENT = "#9184d9"
GOOD = "#5fae8c"
BAD = "#cf7f77"

# Same faces as app.css's body stack, but Roboto is promoted to the front: the
# app itself resolves to Roboto on Android (where this app is actually used),
# while mail clients there don't know -apple-system/BlinkMacSystemFont and some
# (Gmail) skip past unknown names inconsistently rather than falling through.
# Leading with the face that will actually match keeps the digest looking like
# the app on the same phone. Single quotes around Segoe UI on purpose: every
# style= attribute here is double-quoted, so a double-quoted font name would
# close the attribute early and silently drop everything after it.
FONT = "Roboto, -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"


def _section(heading: str, lines: list[str]) -> str:
    items = "".join(
        f'<p style="margin:0 0 6px;font-family:{FONT};font-size:15px;line-height:1.5;color:{TEXT};">{escape(line)}</p>'
        if line
        else '<div style="height:6px;"></div>'
        for line in lines
    )
    return (
        f'<tr><td style="font-family:{FONT};padding:0 0 18px;">'
        f'<p style="margin:0 0 6px;font-family:{FONT};font-size:12px;font-weight:700;letter-spacing:0.04em;'
        f'text-transform:uppercase;color:{TEXT_FAINT};">{escape(heading)}</p>'
        f"{items}"
        "</td></tr>"
    )


def render(
    title: str,
    sections: list[tuple[str, list[str]]],
    *,
    hero_label: str | None = None,
    hero_value: str | None = None,
    hero_tone: str = "neutral",
    footer: str | None = None,
) -> str:
    """Wrap sections in the app's dark card layout.

    hero_tone: "neutral" | "good" | "bad" -- colors the big figure the way the
    dashboard's safe-to-spend hero does (negative in --bad, otherwise plain).
    """
    hero_html = ""
    if hero_value is not None:
        color = {"good": GOOD, "bad": BAD}.get(hero_tone, TEXT)
        hero_html = (
            f'<tr><td style="font-family:{FONT};padding:0 0 20px;">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="background:{BG_ELEVATED};border:1px solid {BORDER};border-radius:8px;">'
            f'<tr><td style="font-family:{FONT};padding:18px 20px;">'
            f'<p style="margin:0 0 4px;font-family:{FONT};font-size:13px;font-weight:500;color:{TEXT_MUTED};">{escape(hero_label or "")}</p>'
            f'<p style="margin:0;font-family:{FONT};font-size:36px;font-weight:800;letter-spacing:-0.03em;line-height:1.1;color:{color};">{escape(hero_value)}</p>'
            "</td></tr></table></td></tr>"
        )

    sections_html = "".join(_section(heading, lines) for heading, lines in sections)
    footer_html = (
        f'<tr><td style="font-family:{FONT};padding:14px 0 0;border-top:1px solid {BORDER};">'
        f'<p style="margin:0;font-family:{FONT};font-size:12px;color:{TEXT_FAINT};">{escape(footer)}</p></td></tr>'
        if footer
        else ""
    )

    return (
        "<!doctype html>"
        '<html lang="en">'
        "<head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="dark">'
        '<meta name="supported-color-schemes" content="dark">'
        f"<title>{escape(title)}</title>"
        "<style>:root{color-scheme:dark;supported-color-schemes:dark;}</style>"
        "</head>"
        f'<body style="margin:0;padding:0;background:{BG};color:{TEXT};font-family:{FONT};">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{BG};">'
        f'<tr><td align="center" style="font-family:{FONT};padding:24px 16px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;">'
        f'<tr><td style="font-family:{FONT};padding:0 0 16px;">'
        f'<p style="margin:0;font-family:{FONT};font-size:20px;font-weight:700;color:{TEXT};">{escape(title)}</p>'
        "</td></tr>"
        f"{hero_html}"
        f'<tr><td style="font-family:{FONT};">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="background:{BG_ELEVATED};border:1px solid {BORDER};border-radius:8px;">'
        f'<tr><td style="font-family:{FONT};padding:18px 20px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        f"{sections_html}{footer_html}"
        "</table></td></tr></table>"
        "</td></tr>"
        "</table></td></tr></table></body></html>"
    )
