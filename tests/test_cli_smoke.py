from sutra_cli.main import build_parser


def test_parser_builds():
    parser = build_parser()
    assert parser.prog == "sutra"
