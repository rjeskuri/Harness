"""High-confidence secret patterns used by the edit hook and the model input filter."""

import re

# (name, pattern), most specific first; a line is reported once, under the first match.
# The sk- patterns may not follow a letter, digit, "_" or "-", so kebab-case words that
# end in "sk" ("task-", "risk-", "flask-") are not mistaken for keys. Only the generic
# assignment matches in any case (API_KEY = "...", apiKey: "..."); the provider formats
# are case-sensitive.
SECRET_PATTERNS = [
    ("AWS access key id", r"AKIA[0-9A-Z]{16}"),
    ("GitHub token", r"gh[pousr]_[A-Za-z0-9]{36,}"),
    ("GitHub fine-grained token", r"github_pat_[A-Za-z0-9_]{22,}"),
    ("Slack token", r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    ("Google API key", r"AIza[0-9A-Za-z_-]{35}"),
    ("Anthropic API key", r"(?<![A-Za-z0-9_-])sk-ant-[A-Za-z0-9_-]{20,}"),
    ("OpenAI API key", r"(?<![A-Za-z0-9_-])sk-(proj-)?[A-Za-z0-9_-]{20,}"),
    ("Stripe live secret key", r"(?<![A-Za-z0-9_-])sk_live_[A-Za-z0-9]{16,}"),
    ("private key block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("Jotform-style apiKey", r"apiKey=[0-9a-f]{32}"),
    (
        "generic secret assignment",
        r"""(api[_-]?key|secret|token|password)\s*[:=]\s*["'][A-Za-z0-9_\-]{24,}["']""",
    ),
]
IGNORE_CASE = {"generic secret assignment"}
COMPILED = [
    (name, re.compile(pattern, re.IGNORECASE if name in IGNORE_CASE else 0))
    for name, pattern in SECRET_PATTERNS
]
