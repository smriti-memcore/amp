def pytest_addoption(parser):
    parser.addoption(
        "--server-cmd",
        default="python examples/minimal_server.py",
        help="Shell command to launch the AMP server under test",
    )
