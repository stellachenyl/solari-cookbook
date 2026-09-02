"""Test doubles. FakeSolariRunner implements the exact runner surface the
routes consume (create_session / kill_session / kill_by_sandbox_id /
run_python / read_file / artifact_dir), mirroring the verified SDK shapes
(RunCodeResult-like item lists) rather than inventing behavior.

Package marker so `pytest exekit/tests` and `import tests.fakes` both work.
"""
