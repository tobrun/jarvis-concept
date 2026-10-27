import subprocess

import gh_read
import pytest


class RecordingGh:
    """A fake gh binary: records each command and answers with a canned body."""

    def __init__(self, stdout: str = "[]") -> None:
        self.commands: list[list[str]] = []
        self.stdout = stdout

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess:
        self.commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=self.stdout, stderr="")


def test_pull_list_path_is_read_with_a_get(capsys):
    gh = RecordingGh('[{"number": 7}]')
    assert gh_read.main(["repos/o/r/pulls"], runner=gh) == 0
    assert gh.commands == [["gh", "api", "--method", "GET", "repos/o/r/pulls"]]
    assert capsys.readouterr().out == '[{"number": 7}]'


@pytest.mark.parametrize(
    "argv",
    [
        ["repos/o/r/issues/1/comments", "-f", "body=hello"],
        ["repos/o/r/issues/1/comments", "--input", "body.json"],
        ["-X", "POST", "repos/o/r/issues/1/comments"],
        ["--method", "POST", "repos/o/r/issues/1/comments"],
    ],
)
def test_comment_path_with_a_body_or_write_verb_is_refused_without_calling_gh(argv):
    gh = RecordingGh()
    assert gh_read.main(argv, runner=gh) == 2
    assert gh.commands == []


def test_hostname_flag_is_refused_without_calling_gh():
    gh = RecordingGh()
    assert gh_read.main(["--hostname", "evil.example.com", "repos/o/r/pulls"], runner=gh) == 2
    assert gh_read.main(["--hostname=evil.example.com"], runner=gh) == 2
    assert gh.commands == []


@pytest.mark.parametrize("path", ["graphql", "/graphql", "GraphQL?query=mutation"])
def test_graphql_is_refused_without_calling_gh(path):
    gh = RecordingGh()
    assert gh_read.main([path], runner=gh) == 2
    assert gh.commands == []


@pytest.mark.parametrize(
    "path",
    [
        "repos/o/r/collaborators",
        "repos/o/r/pulls/../../../user/repos",
        "https://evil.example.com/x",
        "orgs/o",
    ],
)
def test_paths_outside_the_read_patterns_are_refused(path):
    with pytest.raises(gh_read.Refused):
        gh_read.decide([path])


@pytest.mark.parametrize(
    "path",
    [
        "repos/o/r/pulls/12/reviews",
        "repos/o/r/issues/3/comments",
        "repos/o/r/commits?since=2026-10-25T00:00:00Z",
        "search/issues?q=is:pr+review-requested:ada",
        "user",
        "notifications",
    ],
)
def test_read_patterns_are_allowed_as_get(path):
    assert gh_read.decide([path]) == ["gh", "api", "--method", "GET", path]


def test_query_string_is_not_judged_as_part_of_the_route():
    path = "search/issues?q=graphql+..+is:pr"
    assert gh_read.decide([path]) == ["gh", "api", "--method", "GET", path]
