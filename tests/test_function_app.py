"""Smoke test: the Functions app imports and registers every function."""


def test_all_functions_registered():
    import function_app

    names = {f.get_function_name() for f in function_app.app.get_functions()}
    assert names == {
        "start_refresh",
        "scheduled_refresh",
        "refresh_all",
        "refresh_dataset",
        "trigger_refresh",
        "get_refresh_status",
        "send_summary",
    }
