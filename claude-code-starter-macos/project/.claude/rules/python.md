---
paths:
  - "**/*.py"
  - "**/*.pyi"
---
# Python conventions

- Type-hint public functions. Pyright runs on `src/`, and hints are what let it catch mistakes early.
- Keep data transforms as pure functions (DataFrame in, DataFrame out) so they can be tested with small fixtures.
- Use `pathlib.Path` for paths. String-joined paths break on spaces and special characters, and again when the code runs on Linux in CI or on a server.
- Catch specific exceptions and let unexpected ones surface. A bare `except:` hides the bugs the tests exist to find.
- Don't add abstractions, config options or error handling for cases that can't happen here. Match the scope of the request.
- Match the style of the surrounding code. When the existing pattern looks wrong, point it out rather than silently diverging.
