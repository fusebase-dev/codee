import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from codee_agent_abstract.provider import AgentModel
from codee_agent_claude_code.provider import ClaudeCodeAgent
from codee_agent_github_copilot.provider import GitHubCopilotAgent

from codee.admin_service import (
    AdminService, azure_oauth, parse_skill, repository_name)
from codee_main_context.context import (
    CodeeMainContext, CodingAgent, Settings, TasksProvider, save_settings)


def _empty_workflow() -> dict:
    return {issue_type: {"nodes": [], "edges": [], "warnings": []}
            for issue_type in ("story", "task")}


def _write_issue_skill(root: Path) -> Path:
    """Create one issue-trigger skill, the input the workflow cache is keyed on."""
    skills_dir = root / ".claude" / "skills"
    skill_dir = skills_dir / "develop"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: develop\ndisable-model-invocation: true\n"
        "x-codee-trigger: issue\nx-codee-issue-status: [Ready]\n"
        "x-codee-issue-type: story\n---\n"
        "After implementation, move the issue to Review.\n"
    )
    return skills_dir


class AdminServiceIssueTriggerTest(unittest.TestCase):
    def test_list_skills_includes_issue_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            skills_dir = Path(temporary_directory)
            skill_dir = skills_dir / "triage"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: triage\ndescription: Triage issues\n"
                "x-codee-trigger: issue\n"
                "x-codee-issue-status: [Ready, In progress]\n"
                "x-codee-issue-type: story\n---\nBody\n"
            )
            service = AdminService.__new__(AdminService)
            service.skills_dir = skills_dir

            skills = service.list_skills()

            self.assertEqual(skills[0]["issue_status"], "Ready, In progress")
            self.assertEqual(skills[0]["issue_type"], "story")

    def test_save_issue_trigger_writes_required_frontmatter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            skills_dir = Path(temporary_directory)
            skill_dir = skills_dir / "triage"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: triage\nx-codee-issue-type: story\n---\nBody\n"
            )
            service = AdminService.__new__(AdminService)
            service.skills_dir = skills_dir

            with patch.object(service, "_write_and_push",
                              return_value=(True, True, "saved")) as write:
                ok, _, _, slug = service.save_skill({
                    "slug": "triage",
                    "name": "triage",
                    "description": "Triage matching issues",
                    "type": "issue trigger",
                    "issue_status": "Ready, In progress",
                    "issue_type": "task",
                    "body": "Body",
                })

            frontmatter, _ = parse_skill(write.call_args.args[1])
            self.assertTrue(ok)
            self.assertEqual(slug, "triage")
            self.assertIs(frontmatter["disable-model-invocation"], True)
            self.assertEqual(frontmatter["x-codee-trigger"], "issue")
            self.assertEqual(
                frontmatter["x-codee-issue-status"], ["Ready", "In progress"])
            self.assertEqual(frontmatter["x-codee-issue-type"], "task")

    def test_save_reports_saved_when_git_push_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skills_dir = root / ".claude" / "skills"
            skill_dir = skills_dir / "triage"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("---\nname: triage\n---\nOld\n")
            service = AdminService.__new__(AdminService)
            service.root = root
            service.skills_dir = skills_dir

            with patch.object(service, "_git_push", return_value=(False, "no upstream")):
                saved, pushed, message, slug = service.save_skill({
                    "slug": "triage",
                    "name": "triage",
                    "description": "Triage matching issues",
                    "type": "knowledge",
                    "issue_status": "",
                    "issue_type": "",
                    "body": "New body",
                })

            self.assertTrue(saved)
            self.assertFalse(pushed)
            self.assertIn("Git push failed", message)
            self.assertEqual(slug, "triage")
            self.assertIn("New body", (skill_dir / "SKILL.md").read_text())
            self.assertEqual(
                service.list_skills()[0]["description"], "Triage matching issues")

    def test_load_issue_trigger_includes_issue_type(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            skills_dir = Path(temporary_directory)
            skill_dir = skills_dir / "triage"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: triage\nx-codee-trigger: issue\n"
                "x-codee-issue-type: story\n---\nBody\n"
            )
            service = AdminService.__new__(AdminService)
            service.skills_dir = skills_dir

            skill = service.load_skill("triage")

            self.assertEqual(skill["issue_type"], "story")

    def test_delete_skill_removes_directory_and_pushes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            skills_dir = Path(temporary_directory)
            skill_dir = skills_dir / "triage"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("---\nname: triage\n---\nBody\n")
            service = AdminService.__new__(AdminService)
            service.skills_dir = skills_dir

            with patch.object(service, "_git_push", return_value=(True, "")) as push:
                deleted, pushed, message = service.delete_skill("triage")

            self.assertTrue(deleted)
            self.assertTrue(pushed)
            self.assertEqual(message, "Deleted triage")
            self.assertFalse(skill_dir.exists())
            self.assertEqual(push.call_args.args[0], "skill: delete triage")

    def test_delete_skill_reports_deleted_when_git_push_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            skills_dir = Path(temporary_directory)
            skill_dir = skills_dir / "triage"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("---\nname: triage\n---\nBody\n")
            service = AdminService.__new__(AdminService)
            service.skills_dir = skills_dir

            with patch.object(service, "_git_push", return_value=(False, "no upstream")):
                deleted, pushed, message = service.delete_skill("triage")

            self.assertTrue(deleted)
            self.assertFalse(pushed)
            self.assertIn("Git push failed", message)
            self.assertFalse(skill_dir.exists())

    def test_delete_skill_rejects_unknown_slug(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = AdminService.__new__(AdminService)
            service.skills_dir = Path(temporary_directory)

            with patch.object(service, "_git_push") as push:
                deleted, pushed, message = service.delete_skill("missing")

            self.assertFalse(deleted)
            self.assertFalse(pushed)
            self.assertEqual(message, "missing does not exist")
            push.assert_not_called()

    def test_resolve_skill_slug_matches_name_and_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            skills_dir = Path(temporary_directory)
            skill_dir = skills_dir / "task-developer"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: Task Developer\ndescription: Build tasks\n---\nBody\n"
            )
            service = AdminService.__new__(AdminService)
            service.skills_dir = skills_dir

            self.assertEqual(
                service.resolve_skill_slug("Task Developer"), "task-developer")
            self.assertEqual(
                service.resolve_skill_slug(" task-developer "), "task-developer")
            self.assertEqual(service.resolve_skill_slug("missing"), "")
            self.assertEqual(service.resolve_skill_slug(""), "")

    def test_generate_workflow_builds_react_flow_graph_from_issue_skills(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skills_dir = root / ".claude" / "skills"
            skill_dir = skills_dir / "develop"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: develop\ndisable-model-invocation: true\n"
                "x-codee-trigger: issue\nx-codee-issue-status: [Ready]\n"
                "x-codee-issue-type: story\n---\n"
                "After implementation, move the issue to Review.\n"
            )
            task_develop_dir = skills_dir / "task-develop"
            task_develop_dir.mkdir()
            (task_develop_dir / "SKILL.md").write_text(
                "---\nname: task-develop\ndisable-model-invocation: true\n"
                "x-codee-trigger: issue\nx-codee-issue-status: [Ready]\n"
                "x-codee-issue-type: task\n---\n"
                "When implementation is complete, move the task to Review.\n"
            )
            review_dir = skills_dir / "review"
            review_dir.mkdir()
            (review_dir / "SKILL.md").write_text(
                "---\nname: review\ndisable-model-invocation: true\n"
                "x-codee-trigger: issue\nx-codee-issue-status: [Review]\n"
                "x-codee-issue-type: story\n---\n"
                "After approval, move the issue to Done.\n"
                "When fixes are needed, move the issue to Ready.\n"
            )
            agent = Mock()
            agent.run.side_effect = [
                '```json\n{"statuses":["Ready","Review","Done"],'
                '"transitions":[{"source":"Ready","target":"Review",'
                '"label":"develop","evidence":"After implementation, move the issue to Review."},'
                '{"source":"Review","target":"Done","label":"review",'
                '"evidence":"After approval, move the issue to Done."},'
                '{"source":"Review","target":"Ready","label":"review",'
                '"evidence":"When fixes are needed, move the issue to Ready."}],'
                '"final_statuses":["Done"]}\n```',
                '{"statuses":["Ready","Review"],"transitions":['
                '{"source":"Ready","target":"Review","label":"task-develop",'
                '"evidence":"When implementation is complete, move the task to Review."}],'
                '"final_statuses":["Review"]}',
            ]
            agent_type = Mock(return_value=agent)
            service = AdminService.__new__(AdminService)
            service.root = root
            service.skills_dir = skills_dir
            service.data_dir = root / ".codee"
            service.context = Mock(settings=Settings(
                coding_agent=CodingAgent.CLAUDE_CODE))

            with patch.dict("codee.admin_service._CODING_AGENTS", {
                CodingAgent.CLAUDE_CODE: agent_type,
            }):
                workflows = service.generate_workflow()

            workflow = workflows["story"]

            self.assertEqual(
                [
                    node["data"]["label"] for node in workflow["nodes"]
                    if node["data"]["label"]
                ],
                ["Ready", "Review", "Done"],
            )
            self.assertEqual(len(workflow["edges"]), 4)
            self.assertEqual(
                workflow["edges"][0]["data"]["skills"],
                ["develop"],
            )
            self.assertEqual(
                workflow["edges"][0]["label"],
                "develop",
            )
            self.assertEqual(
                workflow["edges"][0]["ariaLabel"],
                "Ready to Review via develop",
            )
            self.assertEqual(workflow["nodes"][1]
                             ["position"], {"x": 440, "y": 0})
            self.assertEqual(workflow["nodes"][1]["sourcePosition"], "right")
            route_node = workflow["nodes"][3]
            self.assertEqual(route_node["id"], "return-route-0")
            self.assertGreater(route_node["position"]["y"], 0)
            self.assertEqual(route_node["style"]["opacity"], 1)
            self.assertIn("workflow-route-node--return",
                          route_node["className"])
            self.assertIs(workflow["edges"][2]["animated"], True)
            self.assertEqual(
                workflow["edges"][2]["className"],
                "workflow-edge workflow-edge--return",
            )
            self.assertEqual(
                workflow["edges"][2]["style"]["strokeDasharray"], "8 6")
            self.assertEqual(workflow["edges"][2]["target"], route_node["id"])
            self.assertEqual(workflow["edges"][2]["label"], "review")
            self.assertEqual(workflow["edges"][3]["source"], route_node["id"])
            self.assertEqual(workflow["edges"][3]["target"], "status-0")
            self.assertNotIn("label", workflow["edges"][3])
            self.assertNotIn("markerEnd", workflow["edges"][2])
            self.assertEqual(workflow["warnings"], [])
            story_prompt = agent.run.call_args_list[0].args[0]
            task_prompt = agent.run.call_args_list[1].args[0]
            self.assertIn("Build the story workflow", story_prompt)
            self.assertIn("move the issue to Review", story_prompt)
            self.assertNotIn("task-develop", story_prompt)
            self.assertIn("Build the task workflow", task_prompt)
            self.assertIn("task-develop", task_prompt)
            self.assertNotIn("## Skill: develop", task_prompt)
            self.assertEqual(
                workflows["task"]["edges"][0]["data"]["skills"],
                ["task-develop"],
            )

    def test_generate_workflow_warns_when_final_status_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skills_dir = root / ".claude" / "skills"
            skill_dir = skills_dir / "develop"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: develop\ndisable-model-invocation: true\n"
                "x-codee-trigger: issue\nx-codee-issue-status: [Ready]\n"
                "x-codee-issue-type: story\n---\n"
                "Keep the issue Ready while work remains.\n"
            )
            agent = Mock()
            agent.run.return_value = (
                '{"statuses":["Ready"],"transitions":['
                '{"source":"Ready","target":"Ready","label":"develop",'
                '"evidence":"Keep the issue Ready while work remains."}],'
                '"final_statuses":[]}'
            )
            service = AdminService.__new__(AdminService)
            service.root = root
            service.skills_dir = skills_dir
            service.data_dir = root / ".codee"
            service.context = Mock(
                settings=Settings(coding_agent=CodingAgent.CLAUDE_CODE))

            with patch.dict("codee.admin_service._CODING_AGENTS", {
                CodingAgent.CLAUDE_CODE: Mock(return_value=agent),
            }):
                workflow = service.generate_workflow()["story"]

            self.assertEqual(workflow["warnings"], [
                "No final human-handoff status is defined in the issue skill workflow.",
            ])

    def test_generate_workflow_removes_skill_edge_bypassing_intermediate_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skills_dir = root / ".claude" / "skills"
            skill_dir = skills_dir / "story-developer"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: story-developer\ndisable-model-invocation: true\n"
                "x-codee-trigger: issue\n"
                "x-codee-issue-status: ['[AI] Ready for development', '[AI] In Progress']\n"
                "x-codee-issue-type: story\n"
                "---\nMake sure story in [AI] In Progress status before doing any work.\n"
                "After task is complete move it to [AI] CR Needed.\n"
            )
            agent = Mock()
            agent.run.return_value = (
                '{"statuses":["[AI] Ready for development","[AI] In Progress",'
                '"[AI] CR Needed"],"transitions":['
                '{"source":"[AI] Ready for development","target":"[AI] In Progress",'
                '"label":"story-developer","evidence":"Make sure story in [AI] In Progress status before doing any work."},'
                '{"source":"[AI] In Progress","target":"[AI] CR Needed",'
                '"label":"story-developer","evidence":"After task is complete move it to [AI] CR Needed."},'
                '{"source":"[AI] Ready for development","target":"[AI] CR Needed",'
                '"label":"story-developer","evidence":"After task is complete move it to [AI] CR Needed."}],'
                '"final_statuses":["[AI] CR Needed"]}'
            )
            service = AdminService.__new__(AdminService)
            service.root = root
            service.skills_dir = skills_dir
            service.data_dir = root / ".codee"
            service.context = Mock(
                settings=Settings(coding_agent=CodingAgent.CLAUDE_CODE))

            with patch.dict("codee.admin_service._CODING_AGENTS", {
                CodingAgent.CLAUDE_CODE: Mock(return_value=agent),
            }):
                workflow = service.generate_workflow()["story"]

            self.assertEqual(
                [(edge["source"], edge["target"])
                 for edge in workflow["edges"]],
                [("status-0", "status-1"), ("status-1", "status-2")],
            )
            self.assertIn(
                "do not emit a direct transition that bypasses it",
                agent.run.call_args.args[0],
            )

    def test_generate_workflow_retries_unsupported_transition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skills_dir = root / ".claude" / "skills"
            skill_dir = skills_dir / "story-planner"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: story-planner\ndisable-model-invocation: true\n"
                "x-codee-trigger: issue\n"
                "x-codee-issue-status: ['[AI] Decomposition needed']\n"
                "x-codee-issue-type: story\n---\n"
                "After planning, move the story to [AI] Ready for human review.\n"
            )
            agent = Mock()
            agent.run.side_effect = [
                '{"statuses":["[AI] Decomposition needed",'
                '"[AI] Ready for development","[AI] Ready for human review"],'
                '"transitions":[{"source":"[AI] Decomposition needed",'
                '"target":"[AI] Ready for development","label":"",'
                '"evidence":""}],"final_statuses":[]}',
                '{"statuses":["[AI] Decomposition needed",'
                '"[AI] Ready for development","[AI] Ready for human review"],'
                '"transitions":[{'
                '"source":"[AI] Decomposition needed",'
                '"target":"[AI] Ready for human review",'
                '"label":"story-planner",'
                '"evidence":"After planning, move the story to [AI] Ready for human review."}],'
                '"final_statuses":["[AI] Ready for human review"]}',
            ]
            service = AdminService.__new__(AdminService)
            service.root = root
            service.skills_dir = skills_dir
            service.data_dir = root / ".codee"
            service.context = Mock(
                settings=Settings(coding_agent=CodingAgent.CLAUDE_CODE))

            with patch.dict("codee.admin_service._CODING_AGENTS", {
                CodingAgent.CLAUDE_CODE: Mock(return_value=agent),
            }):
                workflow = service.generate_workflow()["story"]

            self.assertEqual(agent.run.call_count, 2)
            self.assertIn(
                "each transition label must name its defining skill",
                agent.run.call_args.args[0],
            )
            route_out, route_in = workflow["nodes"][3:]
            self.assertEqual(route_out["id"], "forward-route-0-out")
            self.assertEqual(route_in["id"], "forward-route-0-in")
            self.assertIn("workflow-route-node--forward",
                          route_out["className"])
            self.assertEqual(route_out["sourcePosition"], "right")
            self.assertEqual(route_out["targetPosition"], "left")
            self.assertEqual(route_in["sourcePosition"], "right")
            self.assertEqual(route_in["targetPosition"], "left")
            self.assertEqual(route_out["style"]["width"], 1)
            self.assertEqual(route_out["style"]["height"], 1)
            self.assertLess(route_out["position"]["y"], 0)
            self.assertEqual(
                route_out["position"]["y"], route_in["position"]["y"])
            self.assertEqual(workflow["edges"][0]["label"], "story-planner")
            self.assertEqual(workflow["edges"][0]["target"], route_out["id"])
            self.assertEqual(workflow["edges"][1]["source"], route_out["id"])
            self.assertEqual(workflow["edges"][1]["target"], route_in["id"])
            self.assertNotIn("label", workflow["edges"][1])
            self.assertEqual(workflow["edges"][2]["source"], route_in["id"])
            self.assertEqual(workflow["edges"][2]["target"], "status-2")

    def test_generate_workflow_warns_when_statuses_have_no_edges(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skills_dir = root / ".claude" / "skills"
            skill_dir = skills_dir / "develop"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: develop\ndisable-model-invocation: true\n"
                "x-codee-trigger: issue\n"
                "x-codee-issue-status: [Ready, In Progress, Review]\n"
                "x-codee-issue-type: story\n---\n"
                "Work on issues in the configured statuses.\n"
            )
            agent = Mock()
            agent.run.return_value = (
                '{"statuses":["Ready","In Progress","Review"],'
                '"transitions":[],"final_statuses":["Review"]}'
            )
            service = AdminService.__new__(AdminService)
            service.root = root
            service.skills_dir = skills_dir
            service.data_dir = root / ".codee"
            service.context = Mock(
                settings=Settings(coding_agent=CodingAgent.CLAUDE_CODE))

            with patch.dict("codee.admin_service._CODING_AGENTS", {
                CodingAgent.CLAUDE_CODE: Mock(return_value=agent),
            }):
                workflow = service.generate_workflow()["story"]

            self.assertEqual(workflow["warnings"], [
                "Workflow statuses are disconnected: no status transitions were found.",
            ])
            self.assertEqual(workflow["edges"], [])
            self.assertTrue(all(
                "workflow-node--disconnected" in node["className"]
                for node in workflow["nodes"]
            ))

    def test_generate_workflow_flags_unhandled_status_on_its_node(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skills_dir = root / ".claude" / "skills"
            skill_dir = skills_dir / "develop"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: develop\ndisable-model-invocation: true\n"
                "x-codee-trigger: issue\nx-codee-issue-status: [Ready]\n"
                "x-codee-issue-type: story\n---\n"
                "Move the issue to In Progress when work starts.\n"
            )
            agent = Mock()
            agent.run.return_value = (
                '{"statuses":["Ready","In Progress","Done"],"transitions":['
                '{"source":"Ready","target":"In Progress","label":"develop",'
                '"evidence":"Move the issue to In Progress when work starts."}],'
                '"final_statuses":["Done"]}'
            )
            service = AdminService.__new__(AdminService)
            service.root = root
            service.skills_dir = skills_dir
            service.data_dir = root / ".codee"
            service.context = Mock(
                settings=Settings(coding_agent=CodingAgent.CLAUDE_CODE))

            with patch.dict("codee.admin_service._CODING_AGENTS", {
                CodingAgent.CLAUDE_CODE: Mock(return_value=agent),
            }):
                workflow = service.generate_workflow()["story"]

            self.assertEqual(workflow["warnings"], [])
            flagged = [
                node["data"]["label"] for node in workflow["nodes"]
                if "workflow-node--unhandled" in node.get("className", "")
            ]
            self.assertEqual(flagged, ["In Progress"])

    def test_generate_workflow_returns_empty_without_issue_skills(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            service = AdminService.__new__(AdminService)
            service.skills_dir = root
            service.data_dir = root / ".codee"

            self.assertEqual(service.generate_workflow(), {
                "story": {"nodes": [], "edges": [], "warnings": []},
                "task": {"nodes": [], "edges": [], "warnings": []},
            })

    def test_generate_workflow_is_cached_until_forced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            service = AdminService.__new__(AdminService)
            service.skills_dir = root / ".claude" / "skills"
            service.data_dir = root / ".codee"
            generated = _empty_workflow()

            with patch.object(service, "_generate_workflow",
                              return_value=generated) as generate:
                self.assertIs(service.generate_workflow(), generated)
                self.assertIs(service.generate_workflow(), generated)
                self.assertIs(service.generate_workflow(force=True), generated)

            self.assertEqual(generate.call_count, 2)

    def test_generate_workflow_reuses_graph_stored_by_an_earlier_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skills_dir = _write_issue_skill(root)
            generated = _empty_workflow()
            generated["story"]["warnings"] = ["stored"]

            first = AdminService.__new__(AdminService)
            first.skills_dir = skills_dir
            first.data_dir = root / ".codee"
            with patch.object(first, "_generate_workflow", return_value=generated):
                first.generate_workflow()

            # A restart starts from an empty in-memory cache, so only the file
            # written above can spare it another coding-agent run.
            restarted = AdminService.__new__(AdminService)
            restarted.skills_dir = skills_dir
            restarted.data_dir = root / ".codee"
            with patch.object(restarted, "_generate_workflow") as generate:
                workflow = restarted.generate_workflow()

            generate.assert_not_called()
            self.assertEqual(workflow["story"]["warnings"], ["stored"])

    def test_generate_workflow_ignores_stored_graph_after_a_skill_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skills_dir = _write_issue_skill(root)

            first = AdminService.__new__(AdminService)
            first.skills_dir = skills_dir
            first.data_dir = root / ".codee"
            with patch.object(first, "_generate_workflow",
                              return_value=_empty_workflow()):
                first.generate_workflow()

            (skills_dir / "develop" / "SKILL.md").write_text(
                "---\nname: develop\ndisable-model-invocation: true\n"
                "x-codee-trigger: issue\nx-codee-issue-status: [Ready]\n"
                "x-codee-issue-type: story\n---\n"
                "After implementation, move the issue to Done.\n"
            )
            regenerated = _empty_workflow()
            regenerated["story"]["warnings"] = ["regenerated"]
            restarted = AdminService.__new__(AdminService)
            restarted.skills_dir = skills_dir
            restarted.data_dir = root / ".codee"
            with patch.object(restarted, "_generate_workflow",
                              return_value=regenerated) as generate:
                workflow = restarted.generate_workflow()

            generate.assert_called_once_with()
            self.assertEqual(workflow["story"]["warnings"], ["regenerated"])

    def test_generate_workflow_ignores_a_corrupt_stored_graph(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skills_dir = _write_issue_skill(root)
            data_dir = root / ".codee"
            data_dir.mkdir()
            (data_dir / "workflow.json").write_text("{ not json")

            service = AdminService.__new__(AdminService)
            service.skills_dir = skills_dir
            service.data_dir = data_dir
            generated = _empty_workflow()
            with patch.object(service, "_generate_workflow",
                              return_value=generated) as generate:
                self.assertIs(service.generate_workflow(), generated)

            generate.assert_called_once_with()


class AdminServiceSkillModelTest(unittest.TestCase):
    def _service(self, skills_dir: Path, frontmatter: str) -> AdminService:
        skill_dir = skills_dir / "nightly"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(f"---\n{frontmatter}---\nBody\n")
        service = AdminService.__new__(AdminService)
        service.skills_dir = skills_dir
        return service

    def test_save_writes_the_model_frontmatter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            skills_dir = Path(temporary_directory)
            service = self._service(skills_dir, "name: nightly\n")

            with patch.object(service, "_write_and_push",
                              return_value=(True, True, "saved")) as write:
                service.save_skill({
                    "slug": "nightly", "name": "nightly", "description": "",
                    "type": "knowledge", "model": "claude-opus-5",
                    "body": "Body",
                })

            frontmatter, _ = parse_skill(write.call_args.args[1])
            self.assertEqual(frontmatter["model"], "claude-opus-5")

    def test_an_empty_model_is_left_out_of_the_frontmatter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            skills_dir = Path(temporary_directory)
            service = self._service(skills_dir, "name: nightly\n")

            with patch.object(service, "_write_and_push",
                              return_value=(True, True, "saved")) as write:
                service.save_skill({
                    "slug": "nightly", "name": "nightly", "description": "",
                    "type": "knowledge", "model": "  ", "body": "Body",
                })

            frontmatter, _ = parse_skill(write.call_args.args[1])
            self.assertNotIn("model", frontmatter)

    def test_load_returns_the_model_and_keeps_it_out_of_preserved_extras(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            skills_dir = Path(temporary_directory)
            service = self._service(
                skills_dir, "name: nightly\nmodel: claude-opus-5\nlicense: MIT\n")

            skill = service.load_skill("nightly")

            self.assertEqual(skill["model"], "claude-opus-5")
            # Managed keys are rewritten on save, so only `license` is carried over.
            self.assertEqual(skill["extra"], "license: MIT\n")

    def test_load_reports_no_model_when_the_skill_declares_none(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            skills_dir = Path(temporary_directory)
            service = self._service(skills_dir, "name: nightly\n")

            self.assertEqual(service.load_skill("nightly")["model"], "")


class AdminServiceSkillExtraFrontmatterTest(unittest.TestCase):
    def _service(self, skills_dir: Path, frontmatter: str) -> AdminService:
        skill_dir = skills_dir / "nightly"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(f"---\n{frontmatter}---\nBody\n")
        service = AdminService.__new__(AdminService)
        service.skills_dir = skills_dir
        return service

    def _save(self, service: AdminService, extra: str | None) -> tuple[Mock, tuple]:
        skill: dict[str, str] = {
            "slug": "nightly", "name": "nightly", "description": "",
            "type": "knowledge", "model": "", "body": "Body",
        }
        if extra is not None:
            skill["extra"] = extra
        with patch.object(service, "_write_and_push",
                          return_value=(True, True, "saved")) as write:
            result = service.save_skill(skill)
        return write, result

    def test_save_writes_the_extra_fields_into_the_frontmatter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory), "name: nightly\n")

            write, _ = self._save(
                service, "allowed-tools: Bash\ncompatibility: Claude Code\n")

            frontmatter, _ = parse_skill(write.call_args.args[1])
            self.assertEqual(frontmatter["allowed-tools"], "Bash")
            self.assertEqual(frontmatter["compatibility"], "Claude Code")

    def test_empty_extra_fields_drop_what_the_file_carried(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(
                Path(temporary_directory), "name: nightly\nlicense: MIT\n")

            write, _ = self._save(service, "")

            frontmatter, _ = parse_skill(write.call_args.args[1])
            self.assertNotIn("license", frontmatter)

    def test_a_caller_that_omits_extra_keeps_the_fields_on_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(
                Path(temporary_directory), "name: nightly\nlicense: MIT\n")

            write, _ = self._save(service, None)

            frontmatter, _ = parse_skill(write.call_args.args[1])
            self.assertEqual(frontmatter["license"], "MIT")

    def test_invalid_yaml_is_reported_and_nothing_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory), "name: nightly\n")

            write, (saved, _, message, slug) = self._save(service, "allowed-tools")

            write.assert_not_called()
            self.assertFalse(saved)
            self.assertIn("key: value", message)
            self.assertEqual(slug, "nightly")

    def test_a_field_that_has_its_own_control_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory), "name: nightly\n")

            write, (saved, _, message, _) = self._save(
                service, "model: claude-opus-5\n")

            write.assert_not_called()
            self.assertFalse(saved)
            self.assertIn("model", message)

    def test_load_returns_the_extra_fields_as_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(
                Path(temporary_directory),
                "name: nightly\nallowed-tools: Bash\nlicense: MIT\n")

            self.assertEqual(service.load_skill("nightly")["extra"],
                             "allowed-tools: Bash\nlicense: MIT\n")

    def test_load_returns_an_empty_string_when_there_are_none(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory), "name: nightly\n")

            self.assertEqual(service.load_skill("nightly")["extra"], "")


class AdminServiceAgentModelsTest(unittest.TestCase):
    def _service(self, agent: CodingAgent) -> AdminService:
        service = AdminService.__new__(AdminService)
        service.context = CodeeMainContext(
            data_dir=Path("/tmp"), settings=Settings(coding_agent=agent))
        service._models_cache = {}
        service._models_lock = threading.Lock()
        return service

    def test_lists_the_configured_agents_models_once_and_caches_them(self) -> None:
        service = self._service(CodingAgent.CLAUDE_CODE)

        with patch.object(ClaudeCodeAgent, "list_models",
                          return_value=[AgentModel("claude-opus-5", "Claude Opus 5")]
                          ) as list_models:
            first = service.list_agent_models()
            second = service.list_agent_models()

        self.assertEqual(first, [{"id": "claude-opus-5", "name": "Claude Opus 5"}])
        self.assertEqual(second, first)
        list_models.assert_called_once()

    def test_an_agent_that_cannot_be_asked_yields_an_empty_list(self) -> None:
        service = self._service(CodingAgent.GITHUB_COPILOT)

        with patch.object(GitHubCopilotAgent, "list_models",
                          side_effect=RuntimeError("copilot is not logged in")):
            self.assertEqual(service.list_agent_models(), [])


class AdminServiceAgentsFileTest(unittest.TestCase):
    def _service(self, root: Path) -> AdminService:
        service = AdminService.__new__(AdminService)
        service.root = root
        service.skills_dir = root / ".claude" / "skills"
        service.memory_dir = root / "memory"
        service.agents_file = root / "AGENTS.md"
        return service

    def test_load_agents_returns_file_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "AGENTS.md").write_text("---\nnot: frontmatter\n---\nBody\n")
            service = self._service(root)

            self.assertEqual(service.load_agents(),
                             "---\nnot: frontmatter\n---\nBody\n")

    def test_load_agents_returns_empty_string_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))

            self.assertEqual(service.load_agents(), "")

    def test_save_agents_writes_text_and_pushes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "AGENTS.md").write_text("Old\n")
            service = self._service(root)

            with patch.object(service, "_git_push", return_value=(True, "")) as push:
                saved, pushed, message = service.save_agents("New rules\n")

            self.assertTrue(saved)
            self.assertTrue(pushed)
            self.assertIn("AGENTS.md", message)
            self.assertEqual((root / "AGENTS.md").read_text(), "New rules\n")
            push.assert_called_once_with("agents: update AGENTS.md")

    def test_git_push_stages_agents_file_only_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            service = self._service(root)

            with patch("codee.admin_service.subprocess.run") as run:
                run.return_value = Mock(returncode=0, stdout="", stderr="")
                service._git_push("skill: update triage")
                without_agents = run.call_args_list[0].args[0]

                (root / "AGENTS.md").write_text("Rules\n")
                run.reset_mock()
                service._git_push("agents: update AGENTS.md")
                with_agents = run.call_args_list[0].args[0]

            self.assertNotIn(str(root / "AGENTS.md"), without_agents)
            self.assertIn(str(root / "AGENTS.md"), with_agents)


class AdminServiceRepositoriesTest(unittest.TestCase):
    """Clones run for real against a local source repo: no network needed."""

    def _service(self, root: Path) -> AdminService:
        service = AdminService.__new__(AdminService)
        service.root = root
        service.repositories_dir = root / "repositories"
        return service

    def _source_repository(self, path: Path, branch: str) -> Path:
        """A one-commit repository whose default branch is ``branch``."""
        path.mkdir(parents=True)
        self._git(path, "init", "-b", branch)
        (path / "README.md").write_text("source\n")
        self._git(path, "add", "README.md")
        self._git(path, "-c", "user.email=codee@example.com",
                  "-c", "user.name=Codee", "-c", "commit.gpgsign=false",
                  "commit", "-m", "initial")
        return path

    def _git(self, cwd: Path, *arguments: str) -> str:
        result = subprocess.run(["git", "-C", str(cwd), *arguments],
                                capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def test_name_is_taken_from_every_url_form(self) -> None:
        self.assertEqual(repository_name("git@github.com:org/codee.git"), "codee")
        self.assertEqual(repository_name("ssh://git@github.com/org/codee.git"),
                         "codee")
        self.assertEqual(repository_name("https://github.com/org/codee/"), "codee")
        self.assertEqual(repository_name("  "), "")

    def test_add_builds_the_bare_and_worktree_layout(self) -> None:
        for branch in ("main", "master"):
            with self.subTest(branch=branch), \
                    tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                source = self._source_repository(root / "source", branch)
                service = self._service(root)

                added, message, name = service.add_repository(str(source))

                repository = root / "repositories" / "source"
                self.assertTrue(added, message)
                self.assertEqual(name, "source")
                self.assertIn(branch, message)
                self.assertTrue((repository / ".bare").is_dir())
                self.assertEqual((repository / ".git").read_text(),
                                 "gitdir: ./.bare\n")
                # The branch is checked out beside `.bare`, as its own worktree.
                self.assertTrue((repository / branch / "README.md").is_file())
                self.assertEqual(
                    self._git(repository / branch, "rev-parse",
                              "--abbrev-ref", "HEAD"),
                    branch)

    def test_add_fetches_remote_tracking_refs_for_later_worktrees(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = self._source_repository(root / "source", "main")
            service = self._service(root)

            service.add_repository(str(source))

            repository = root / "repositories" / "source"
            self.assertTrue(self._git(repository, "rev-parse", "origin/main"))

    def test_add_refuses_a_repository_that_is_already_there(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "repositories" / "source").mkdir(parents=True)
            service = self._service(root)

            added, message, _ = service.add_repository(
                "git@github.com:org/source.git")

            self.assertFalse(added)
            self.assertIn("already exists", message)

    def test_a_failed_clone_leaves_no_directory_behind(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            service = self._service(root)

            added, message, _ = service.add_repository(
                str(root / "missing-repository.git"))

            self.assertFalse(added)
            self.assertIn("Could not clone", message)
            self.assertFalse((root / "repositories" / "missing-repository")
                             .exists())

    def test_list_reports_the_clone_and_skips_everything_else(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = self._source_repository(root / "source", "main")
            service = self._service(root)
            service.add_repository(str(source))
            (root / "repositories" / "scratch").mkdir()

            repositories = service.list_repositories()

            self.assertEqual([repository["name"] for repository in repositories],
                             ["source"])
            self.assertEqual(repositories[0]["url"], str(source))
            self.assertEqual(repositories[0]["default_branch"], "main")

    def test_list_is_empty_before_anything_is_cloned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))

            self.assertEqual(service.list_repositories(), [])


class AzureDevOpsOAuthTest(unittest.TestCase):
    """The admin side of the flow: build the URL, then handle the callback."""

    AZURE_CREDENTIALS = {
        "organization_url": "https://dev.azure.com/acme",
        "tenant_id": "tenant-1",
        "client_id": "client-1",
        "client_secret": "secret-1",
    }

    def _service(self, data_dir: Path, credentials: dict | None = None) -> AdminService:
        service = AdminService.__new__(AdminService)
        service.data_dir = data_dir
        settings = Settings(
            tasks_provider=TasksProvider.AZURE_DEVOPS,
            credentials={"azure_devops": credentials
                         if credentials is not None else self.AZURE_CREDENTIALS})
        service.context = CodeeMainContext(data_dir=data_dir, settings=settings)
        save_settings(data_dir, settings)
        return service

    def test_redirect_uri_follows_the_admin_port(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))

            with patch.dict(os.environ, {"REFLEX_API_URL": "http://127.0.0.1:9100",
                                         "CODEE_ADMIN_BASE_URL": ""}, clear=False):
                self.assertEqual(service.azure_redirect_uri(),
                                 "http://localhost:9100/api/oauth/azure-devops/callback")

    def test_redirect_uri_honours_an_explicit_base_url(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))

            with patch.dict(os.environ,
                            {"CODEE_ADMIN_BASE_URL": "https://codee.example.com/"},
                            clear=False):
                self.assertEqual(
                    service.azure_redirect_uri(),
                    "https://codee.example.com/api/oauth/azure-devops/callback")

    def test_authorization_refuses_an_incomplete_app_registration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory),
                                    credentials={"organization_url": "https://dev.azure.com/acme"})

            started, message = service.start_azure_authorization()

            self.assertFalse(started)
            self.assertIn("client secret", message)

    def test_callback_stores_the_tokens_for_the_state_it_issued(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))
            started, url = service.start_azure_authorization()
            state = parse_qs(urlparse(url).query)["state"][0]
            tokens = {"access_token": "at", "refresh_token": "rt",
                      "expires_at": "2026-08-05T12:00:00+00:00", "scope": "s"}

            with patch.object(azure_oauth, "exchange_code", return_value=tokens) as exchange, \
                    patch.object(azure_oauth, "fetch_account", return_value="dev@acme.com"):
                connected, message = service.complete_azure_authorization("code-1", state)

            self.assertTrue(started)
            self.assertTrue(connected)
            self.assertIn("dev@acme.com", message)
            # The exchange must reuse the redirect URI the authorization was issued
            # with — Entra rejects the code otherwise.
            self.assertEqual(exchange.call_args.args[1], service.azure_redirect_uri())
            self.assertTrue(service.azure_connection()["connected"])

    def test_callback_with_a_forged_state_stores_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))
            service.start_azure_authorization()

            with patch.object(azure_oauth, "exchange_code") as exchange:
                connected, message = service.complete_azure_authorization(
                    "code-1", "forged-state")

            exchange.assert_not_called()
            self.assertFalse(connected)
            self.assertIn("again", message)
            self.assertFalse(service.azure_connection()["connected"])

    def test_failed_exchange_reports_the_entra_message(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))
            _, url = service.start_azure_authorization()
            state = parse_qs(urlparse(url).query)["state"][0]

            with patch.object(azure_oauth, "exchange_code",
                              side_effect=azure_oauth.AzureDevOpsAuthError(
                                  "AADSTS7000215: Invalid client secret.")):
                connected, message = service.complete_azure_authorization("code-1", state)

            self.assertFalse(connected)
            self.assertIn("Invalid client secret", message)
            self.assertFalse(service.azure_connection()["connected"])

    def test_disconnect_forgets_the_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))
            _, url = service.start_azure_authorization()
            state = parse_qs(urlparse(url).query)["state"][0]
            with patch.object(azure_oauth, "exchange_code",
                              return_value={"access_token": "at", "refresh_token": "rt",
                                            "expires_at": None, "scope": ""}), \
                    patch.object(azure_oauth, "fetch_account", return_value=""):
                service.complete_azure_authorization("code-1", state)

            service.disconnect_azure()

            self.assertFalse(service.azure_connection()["connected"])


class VerifyTasksConnectionTest(unittest.TestCase):
    """The settings page checks credentials by pulling tasks with them."""

    JIRA_CREDENTIALS = {
        "base_url": "https://acme.atlassian.net",
        "account_email": "agent@acme.test",
        "api_token": "token",
        "project": "CORE",
    }

    def _service(self, root: Path) -> AdminService:
        service = AdminService.__new__(AdminService)
        service.root = root
        service.skills_dir = _write_issue_skill(root)
        service.data_dir = root / ".codee"
        service.data_dir.mkdir()
        settings = Settings(credentials={"jira": {"base_url": "https://stale.test"}})
        service.context = CodeeMainContext(
            data_dir=service.data_dir, settings=settings)
        save_settings(service.data_dir, settings)
        return service

    def test_the_form_credentials_are_used_not_the_saved_ones(self) -> None:
        # The point of the check is to try credentials before committing them.
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))
            response = Mock(status_code=200)
            response.json.return_value = {"issues": []}

            with patch("codee_tasks_jira.provider.requests.get",
                       return_value=response) as get:
                verified, message = service.verify_tasks_connection(
                    "jira", self.JIRA_CREDENTIALS)

            self.assertTrue(verified, message)
            self.assertTrue(get.call_args.args[0].startswith(
                "https://acme.atlassian.net"))

    def test_it_polls_the_statuses_the_issue_skills_declare(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))
            response = Mock(status_code=200)
            response.json.return_value = {"issues": []}

            with patch("codee_tasks_jira.provider.requests.get",
                       return_value=response) as get:
                service.verify_tasks_connection("jira", self.JIRA_CREDENTIALS)

            self.assertIn('status in ("Ready")',
                          get.call_args.kwargs["params"]["jql"])

    def test_missing_credentials_are_refused_before_any_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))

            with patch("codee_tasks_jira.provider.requests.get") as get:
                verified, message = service.verify_tasks_connection(
                    "jira", {"base_url": "https://acme.atlassian.net"})

            self.assertFalse(verified)
            self.assertIn("Not configured", message)
            get.assert_not_called()

    def test_an_unknown_provider_is_reported_rather_than_raised(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))

            verified, message = service.verify_tasks_connection("trello", {})

            self.assertFalse(verified)
            self.assertIn("trello", message)

    def test_saved_settings_are_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            service = self._service(Path(temporary_directory))
            response = Mock(status_code=200)
            response.json.return_value = {"issues": []}

            with patch("codee_tasks_jira.provider.requests.get",
                       return_value=response):
                service.verify_tasks_connection("jira", self.JIRA_CREDENTIALS)

            self.assertEqual(service.load_settings().credentials["jira"],
                             {"base_url": "https://stale.test"})


if __name__ == "__main__":
    unittest.main()
