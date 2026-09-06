"""Human-readable description of a 5-field cron expression.

ponytail: delegates to cron_descriptor (handles ranges, lists, AM/PM). Returns
None on anything it can't parse so the UI falls back to the raw expression.
"""

from cron_descriptor import Options, get_description

# The locale is pinned rather than autodetected. Left to cron_descriptor, an
# unset or `C` locale (containers, cron, systemd) makes it warn on import and
# fall back to en_US anyway, and a non-English one gets a half-translated
# description -- `LANG=de_DE` yields "Um 05:00, Tuesday bis Saturday". These
# strings go into the admin UI beside cron expressions that are themselves
# English, so en_US is the answer on every machine.
_OPTS = Options(locale_code="en_US", use_24hour_time_format=False)


def describe_cron(expr):
    if not expr or len(expr.split()) != 5:
        return None
    try:
        return get_description(expr, _OPTS)
    except Exception:
        return None


if __name__ == "__main__":
    cases = {
        "0 5 * * 2-6": "At 05:00 AM, Tuesday through Saturday",
        "*/5 * * * *": "Every 5 minutes",
        "30 9 * * 1": "At 09:30 AM, only on Monday",
        "5,10 * * * *": "At 5 and 10 minutes past the hour",
        "bad": None,
    }
    for expr, want in cases.items():
        got = describe_cron(expr)
        assert got == want, f"{expr!r}: got {got!r}, want {want!r}"
    print("ok")
