---
paths:
  - "**/*.ipynb"
  - "notebooks/**"
---
# Notebooks

- Notebooks are paired with `.py` files (jupytext, percent format). Edit the `.py`, then run `uv run jupytext --sync <file>.py`. A hook blocks direct edits to paired `.ipynb` files.
- Logic that's worth keeping belongs in `src/`, with tests. A notebook should import it, call it, and show results.
- Keep displayed outputs small (`df.head()`, `df.shape`, summary stats). Large outputs get saved into the notebook and cost context every time it's read.
- To check that a notebook still runs top to bottom, execute it headless: `uv run jupyter nbconvert --to notebook --execute <nb> --output-dir .nbout`. Hidden state from an interactive session doesn't count as working.
- Save plots to PNG when you need to check them; you can open the image and read it.
