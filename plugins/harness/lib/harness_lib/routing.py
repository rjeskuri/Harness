"""Model routing every harness tool agrees on: the tier rubric, the five routing settings
(HARNESS_CODE_TIER, HARNESS_CODE_EFFORT, HARNESS_REVIEW_TIER, HARNESS_ULTRAREVIEW and
HARNESS_WAVE_REVIEW), each stage's route and where an escalation goes. See docs/design.md
(Settings, harness_lib).

No rule here picks fable: the labels give sonnet or opus (opus at effort xhigh for `size:L` or
`risk:high`), escalation tops out at opus xhigh, and fable runs only when a setting or a
caller names it.

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
LABEL_EFFORT = "xhigh"  # size:L or risk:high, a risk:high review, and an escalation from opus

_ON = ("on", "1", "true", "yes")
_OFF = ("off", "0", "false", "no")


def label_tier(kind, size, risk):
    """The tier the labels give: `size:S` and `risk:low` and a type other than `bug` is sonnet;
    everything else is opus, `size:L` and `risk:high` included (at effort xhigh, see
    label_effort)."""
    if size == "S" and risk == "low" and kind != "bug":
        return "sonnet"
    return "opus"


def label_effort(size, risk):
    """The coding stages' effort from the labels: xhigh for `size:L` or `risk:high`, else None."""
    return LABEL_EFFORT if size == "L" or risk == "high" else None


def review_label_tier(risk):
    """The review stage's tier from the labels: opus for every risk (at effort xhigh for
    `risk:high`, see review_route)."""
    return "opus"


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


def _code(labels, labels_effort, routing):
    """The coding route from the labels' tier and effort under the settings: HARNESS_CODE_TIER
    replaces the labels' tier and their effort; HARNESS_CODE_EFFORT, when set, is the effort."""
    override = routing.get("code_tier")
    return {
        "tier": override or labels,
        "effort": routing.get("code_effort") or (None if override else labels_effort),
        "from": "HARNESS_CODE_TIER" if override else "labels",
        "labels_tier": labels,
    }


def code_route(kind, size, risk, routing):
    """The coding stage's route: {tier, effort, from, labels_tier}. `from` is
    HARNESS_CODE_TIER when it overrides the labels, else "labels". Effort is
    HARNESS_CODE_EFFORT when set, else the labels' effort (xhigh for `size:L` or `risk:high`)
    unless HARNESS_CODE_TIER replaced the labels, their effort with them."""
    return _code(label_tier(kind, size, risk), label_effort(size, risk), routing)


def cached_code_route(cached_tier, routing):
    """The coding stage's route for a ticket cache that holds a tier and no labels: the cached
    tier stands in for the labels' tier. A cached fable is the pre-#239 rubric's answer for
    `size:L` or `risk:high`, so it stands for opus at xhigh; a cached sonnet or opus carries no
    effort. The settings decide as in code_route."""
    if cached_tier == "fable":
        return _code("opus", LABEL_EFFORT, routing)
    return _code(cached_tier, None, routing)


def review_route(risk, routing):
    """The review stage's route: {tier, effort, from, labels_tier}. `from` is
    HARNESS_REVIEW_TIER when it overrides the labels, else "labels". Effort is xhigh on a
    fable review (fable only ever from HARNESS_REVIEW_TIER) and on a `risk:high` review by the
    labels; an override replaces the labels' effort along with their tier."""
    labels = review_label_tier(risk)
    override = routing.get("review_tier")
    tier = override or labels
    if tier == "fable":
        effort = REVIEW_FABLE_EFFORT
    elif override:
        effort = None
    else:
        effort = LABEL_EFFORT if risk == "high" else None
    return {
        "tier": tier,
        "effort": effort,
        "from": "HARNESS_REVIEW_TIER" if override else "labels",
        "labels_tier": labels,
    }


def next_route(tier, effort):
    """Where an escalation from a stage that ran at (tier, effort) goes, as (tier, effort), or
    None when nothing is above it (needs-input): sonnet goes to opus; opus with no effort or
    below xhigh goes to opus at xhigh; opus at xhigh or max, and an explicitly named fable, top
    out. An escalation never goes to fable."""
    if tier == "sonnet":
        return ("opus", None)
    if tier == "opus" and effort in (None, "low", "medium", "high"):
        return ("opus", LABEL_EFFORT)
    return None
