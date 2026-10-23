"""Model routing every harness tool agrees on: the tier rubric, the five routing settings
(HARNESS_CODE_TIER, HARNESS_CODE_EFFORT, HARNESS_REVIEW_TIER, HARNESS_ULTRAREVIEW and
HARNESS_WAVE_REVIEW) and each stage's route. See docs/design.md (Settings, harness_lib).

Standard library only; works with the python3 from Xcode's command line tools (3.9) or newer.
"""

from __future__ import annotations

from harness_lib.settings import setting, settings_env

TIERS = ("sonnet", "opus", "fable")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
KEYS = (
    "HARNESS_CODE_TIER",
    "HARNESS_CODE_EFFORT",
    "HARNESS_REVIEW_TIER",
    "HARNESS_ULTRAREVIEW",
    "HARNESS_WAVE_REVIEW",
)
WAVE_EFFORT = "xhigh"
REVIEW_FABLE_EFFORT = "xhigh"

_ON = ("on", "1", "true", "yes")
_OFF = ("off", "0", "false", "no")


def label_tier(kind, size, risk):
    """The tier the labels give: `size:L` or `risk:high` is fable; `size:S` and `risk:low`
    and a type other than `bug` is sonnet; everything else is opus."""
    if size == "L" or risk == "high":
        return "fable"
    if size == "S" and risk == "low" and kind != "bug":
        return "sonnet"
    return "opus"


def review_label_tier(risk):
    """The review stage's tier from the labels: fable for `risk:high`, else opus."""
    return "fable" if risk == "high" else "opus"


def _choice(key, file_env, allowed, problems):
    """The setting's value lower-cased when it is one of `allowed`; None when unset, or
    invalid (one line added to `problems`)."""
    raw = setting(key, file_env)
    if raw is None:
        return None
    value = raw.lower()
    if value in allowed:
        return value
    problems.append("%s=%r is not one of %s; ignored" % (key, raw, ", ".join(allowed)))
    return None


def load_routing(main):
    """The routing settings of the project whose main checkout is `main`: the environment
    when not blank, else `env` in <main>/.claude/settings.json (settings.local.json is never
    read). Keys: code_tier, code_effort, review_tier (a tier or None), ultrareview (a bool,
    on by default), wave_tier (a tier or None) and wave_effort (WAVE_EFFORT with a wave_tier,
    else None), and problems (one line per invalid value, which is ignored)."""
    file_env = settings_env(main, ("settings.json",))
    problems = []
    code_tier = _choice("HARNESS_CODE_TIER", file_env, TIERS, problems)
    code_effort = _choice("HARNESS_CODE_EFFORT", file_env, EFFORTS, problems)
    review_tier = _choice("HARNESS_REVIEW_TIER", file_env, TIERS, problems)
    switch = _choice("HARNESS_ULTRAREVIEW", file_env, _ON + _OFF, problems)
    wave = _choice("HARNESS_WAVE_REVIEW", file_env, TIERS + _OFF, problems)
    wave_tier = wave if wave in TIERS else None
    return {
        "code_tier": code_tier,
        "code_effort": code_effort,
        "review_tier": review_tier,
        "ultrareview": switch not in _OFF,
        "wave_tier": wave_tier,
        "wave_effort": WAVE_EFFORT if wave_tier else None,
        "problems": problems,
    }


def code_route(kind, size, risk, routing):
    """The coding stage's route: {tier, effort, from, labels_tier}. `from` is
    HARNESS_CODE_TIER when it overrides the labels, else "labels"; effort is
    HARNESS_CODE_EFFORT or None."""
    labels = label_tier(kind, size, risk)
    override = routing.get("code_tier")
    return {
        "tier": override or labels,
        "effort": routing.get("code_effort"),
        "from": "HARNESS_CODE_TIER" if override else "labels",
        "labels_tier": labels,
    }


def review_route(risk, routing):
    """The review stage's route: {tier, effort, from, labels_tier}. `from` is
    HARNESS_REVIEW_TIER when it overrides the labels, else "labels"; effort is xhigh on a
    fable review and None otherwise."""
    labels = review_label_tier(risk)
    override = routing.get("review_tier")
    tier = override or labels
    return {
        "tier": tier,
        "effort": REVIEW_FABLE_EFFORT if tier == "fable" else None,
        "from": "HARNESS_REVIEW_TIER" if override else "labels",
        "labels_tier": labels,
    }
