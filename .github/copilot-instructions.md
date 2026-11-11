# Repository Context

- This repository is for production Snowflake data engineering work.
- Target Python 3.12 or later.
- Assume enterprise-scale datasets and production usage.
- Generate production-ready code only.
- Never generate toy examples unless explicitly requested.
- Follow existing repository conventions when they are present.
- If repository structure or naming conventions are unclear, infer them from the existing codebase before introducing new patterns.

# Snowflake

- Assume development happens in VS Code with the Snowflake extension.
- Use named connections from `~/.snowflake/connections.toml`.
- Never hardcode:
  - account
  - username
  - password
  - authenticator
  - private keys
  - warehouse
  - database
  - schema
  - role
- Create Snowpark sessions with:
  `Session.builder.config("connection_name", "<connection_name>").create()`
- Assume SQL runs through the active Snowflake connection.
- Never generate login scripts.
- Prefer Snowpark over `snowflake.connector` unless a specific use case requires the connector.
- Push computation into Snowflake whenever practical.
- Avoid collecting large datasets on the client.

# SQL

- Write Snowflake SQL.
- Use uppercase SQL keywords.
- Prefer CTEs for readability.
- Prefer `QUALIFY` where appropriate.
- Prefer window functions where they simplify logic.
- Avoid `SELECT *` unless explicitly requested.
- Write maintainable SQL with clear aliases, stable join logic, and explicit column selection.

# Python

- Generate production-quality Python.
- Use `pathlib` for file paths.
- Use `logging` instead of `print()`.
- Use type hints.
- Use `dataclasses` where they improve clarity.
- Use context managers for resources.
- Prefer reusable functions over duplicated logic.
- Handle exceptions explicitly and provide useful error messages.
- Avoid global state.
- Avoid hardcoded paths.

# Streamlit

- Assume Streamlit in Snowflake.
- Keep UI logic separate from business logic.
- Cache expensive operations.
- Use session state correctly.
- Avoid repeated database queries.
- Prefer simple, testable components.

# Cortex

- Prefer Cortex AI capabilities where appropriate.
- Keep AI processing inside Snowflake unless the task explicitly requires otherwise.

# Performance

Optimise for:
- predicate pushdown
- partition pruning
- minimal scans
- efficient joins
- reusable DataFrames
- minimal network traffic

# Logging

- Use the `logging` module.
- Never use `print()` in production code.
- Provide useful exception messages and log context that helps debugging without exposing secrets.

# Testing

- Generate `pytest` tests when they add value.
- Prefer deterministic tests.
- Mock external services and Snowflake access where appropriate.
- Cover edge cases, failure paths, and data-shape assumptions.

# Documentation

- Add concise docstrings to public functions describing:
  - purpose
  - parameters
  - return values
- Explain why complex logic exists rather than restating the code.
- Keep comments focused on intent, assumptions, and non-obvious trade-offs.

# Coding Style

- Prefer readable code over clever code.
- Minimise dependencies.
- Avoid unnecessary abstraction.
- Prefer the production-ready approach when multiple valid options exist.
- If trade-offs matter, choose one approach and keep the rationale brief in code or docs when needed.

# Repository Structure

- Respect existing folders, module boundaries, and naming conventions.
- Place new app, SQL, and test code where similar code already lives.
- If no structure exists yet, keep the layout simple and conventional.

# Output

- Return production-ready code that can be committed with minimal cleanup.
- Preserve existing useful instructions when editing related files.
- Remove duplicates and conflicts when updating repository guidance.
