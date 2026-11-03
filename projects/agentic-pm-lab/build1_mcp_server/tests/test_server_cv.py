"""End-to-end tests for the CV tools and resource over stdio."""

import asyncio
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

CV = """# Jane Example

## Summary
Platform PM with OAuth and API experience.

## Experience
### PM, Acme (2020-01 to present)
- Built OAuth and SAML identity APIs.

## Right to Work
Example citizen, with the full right to work in the UK.

## Education
BSc Computer Science
"""


@pytest.fixture
def cv_path(tmp_path):
    path = tmp_path / "cv.md"
    path.write_text(CV, encoding="utf-8")
    return path


def run(cv_path, fn):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "build1_mcp_server.server"],
        env={**os.environ, "CV_PATH": str(cv_path)},
    )

    async def go():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await fn(session)

    return asyncio.run(go())


def test_search_and_verify_quote(cv_path):
    async def check(s):
        found = await s.call_tool("search_cv", {"query": "oauth saml"})
        ok = await s.call_tool("verify_quote", {"quote": "Built OAuth and SAML identity APIs."})
        nope = await s.call_tool("verify_quote", {"quote": "Built OAuth APIs for banks."})
        return found.content[0].text, ok.content[0].text, nope.content[0].text

    found, ok, nope = run(cv_path, check)
    assert "Built OAuth and SAML identity APIs." in found and "PM, Acme" in found
    assert ok.startswith("FOUND")
    assert nope.startswith("NOT FOUND") and "Closest CV line" in nope


def test_right_to_work_never_leaves_the_server(cv_path):
    async def check(s):
        resource = (await s.read_resource("cv://master")).contents[0].text
        search = (await s.call_tool("search_cv", {"query": "citizen"})).content[0].text
        quote = (await s.call_tool("verify_quote", {"quote": "full right to work in the UK"})).content[0].text
        return resource, search, quote

    resource, search, quote = run(cv_path, check)
    assert "Right to Work" not in resource and "citizen" not in resource
    assert "## Education" in resource and "## Experience" in resource
    assert search.startswith("No lines contain")
    assert quote.startswith("NOT FOUND")


def test_missing_cv_is_reported(tmp_path):
    async def check(s):
        result = await s.call_tool("search_cv", {"query": "oauth"})
        with pytest.raises(Exception, match="No CV at"):
            await s.read_resource("cv://master")
        return result

    result = run(tmp_path / "absent.md", check)
    assert result.is_error and "No CV at" in result.content[0].text
