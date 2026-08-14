# Contributing

Contributions are welcome through GitHub pull requests.

By submitting a contribution, you confirm that you have the right to provide it and agree that it
will be licensed under the repository's [MIT License](LICENSE). Do not submit secrets, private
operator data, third-party code without compatible licensing, or generated artifacts that cannot
be independently reproduced.

Before opening a pull request, run the repository checks:

```bash
uv sync --locked --all-extras
uv run ruff format --check src tests
uv run ruff check src tests
uv run mypy src
uv run pytest -q
```

Dependencies and other third-party components retain their own copyrights and licenses. New
dependencies must have licensing compatible with distribution of this project under MIT and must
be recorded in the lockfile.
