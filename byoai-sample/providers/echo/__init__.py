"""
providers.echo — package marker for the Echo provider module

Role:
    Marks providers/echo/ as a Python package so bot_echo can be imported
    as providers.echo.bot_echo. Carries no runtime state and performs no
    initialization.

Does not own:
    Echo plugin behavior (owned by bot_echo.py in this package).

Dependencies:
    None.

RCMS lifecycle:
    Phase 1 (Start): not directly involved.
    Phase 2 (During): not directly involved.
    Phase 3 (Closure): not directly involved.

Spec:
    Not applicable — this file is a Python package marker, not a protocol
    handler. See bot_echo.py for the Echo provider's RCMS interactions and
    bridge/schema/rcms.schema.json for the authoritative wire shape.
"""
