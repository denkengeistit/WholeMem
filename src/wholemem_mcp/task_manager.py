"""Task Manager MCP tools — create, update, and list tasks in TASKS.md.

Add to server.py tool registration:

    from wholemem_mcp.task_manager import register_task_manager_tools
    register_task_manager_tools(mcp)

Uses the TaskStore class from tasks/store.py for all task operations.
Tasks use Obsidian Tasks plugin syntax with optional 🆔 task IDs.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Optional

from wholemem_mcp.tasks.store import TaskStore


def register_task_manager_tools(mcp) -> None:
    """Attach task management tools to the FastMCP server."""

    @mcp.tool(
        name="create_task",
        annotations={
            "title": "Create New Task",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    )
    async def create_task(
        workspace: str,
        subject: str,
        description: str,
        task_id: Optional[str] = None,
        due_date: Optional[str] = None,
        assignee: Optional[str] = None,
    ) -> str:
        """Create a new task in TASKS.md in Obsidian Tasks format.

        Creates a task with optional metadata including auto-generated or
        provided task ID, due date, and assignee.

        Args:
            workspace: Absolute path to the workspace directory.
            subject: Brief title/summary of the task.
            description: Detailed description of what needs to be done.
            task_id: Optional unique task ID (🆔). Auto-generated if not provided.
            due_date: Optional due date in YYYY-MM-DD format.
            assignee: Optional assignee name.

        Returns:
            JSON with success status and task details.
        """
        workspace_path = Path(workspace).expanduser()
        task_store = TaskStore(workspace_path)

        # Generate task ID if not provided
        if not task_id:
            task_id = str(uuid.uuid4())[:8]

        # Build task text with subject and description
        task_text = f"{subject}"
        if description:
            task_text += f" — {description}"

        # Add metadata
        if due_date:
            task_text += f" 📅 {due_date}"
        if assignee:
            task_text += f" [assignee:: {assignee}]"
        task_text += f" [status:: pending]"

        try:
            # Call method to add task with ID (it adds 🆔, 📅, [assignee::], [status::])
            task_store.add_task_with_id(
                text=task_text,
                task_id=task_id,
                due_date=due_date,
                assignee=assignee,
                status="pending",
            )

            return json.dumps(
                {
                    "success": True,
                    "task_id": task_id,
                    "subject": subject,
                    "description": description,
                    "due_date": due_date,
                    "assignee": assignee,
                },
                indent=2,
            )
        except Exception as exc:
            return json.dumps(
                {"success": False, "error": str(exc)},
                indent=2,
            )

    @mcp.tool(
        name="update_task",
        annotations={
            "title": "Update Existing Task",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    )
    async def update_task(
        workspace: str,
        task_id: str,
        status: Optional[str] = None,
        description: Optional[str] = None,
        due_date: Optional[str] = None,
        assignee: Optional[str] = None,
    ) -> str:
        """Update an existing task in TASKS.md.

        Can change status (pending/in-progress/completed), update description,
        modify due date, or change assignee.

        Args:
            workspace: Absolute path to the workspace directory.
            task_id: The 🆔 task ID to update.
            status: New status (pending/in-progress/completed).
            description: New description text.
            due_date: New due date in YYYY-MM-DD format.
            assignee: New assignee name.

        Returns:
            JSON with success status and updated task details.
        """
        workspace_path = Path(workspace).expanduser()
        task_store = TaskStore(workspace_path)

        try:
            # Find the task
            task = task_store.get_task_by_id(task_id)
            if task is None:
                return json.dumps(
                    {"success": False, "error": f"No task found with ID '{task_id}'"},
                    indent=2,
                )

            # Handle status changes
            if status:
                status_lower = status.lower()
                if status_lower == "completed":
                    task_store.complete_task(task_id=task_id)
                elif status_lower == "in-progress" and not task.checked:
                    # Remove checked state if re-opening
                    task_store.claim_task(task.line_num, assignee or "agent")
                elif status_lower == "pending" and not task.checked:
                    # Just update the status metadata
                    task_store.update_task_status(task.line_num, "pending")

            # Update other metadata
            if description or due_date or assignee:
                task_store.update_task_metadata(
                    line_num=task.line_num,
                    description=description,
                    due_date=due_date,
                    assignee=assignee,
                )

            # Fetch updated task
            updated_task = task_store.get_task_by_id(task_id)

            return json.dumps(
                {
                    "success": True,
                    "task_id": task_id,
                    "line_num": updated_task.line_num if updated_task else None,
                    "status": status,
                    "description": description,
                    "due_date": due_date,
                    "assignee": assignee,
                },
                indent=2,
            )
        except Exception as exc:
            return json.dumps(
                {"success": False, "error": str(exc)},
                indent=2,
            )

    @mcp.tool(
        name="list_tasks",
        annotations={
            "title": "List Tasks",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def list_tasks(
        workspace: str,
        status: Optional[str] = None,
        assignee: Optional[str] = None,
        include_completed: bool = False,
    ) -> str:
        """List all tasks from TASKS.md, optionally filtered by status.

        Returns a structured JSON list of tasks with their IDs, text,
        status, and metadata.

        Args:
            workspace: Absolute path to the workspace directory.
            status: Filter by status (pending/in-progress/completed).
            assignee: Filter by assignee name.
            include_completed: Include completed tasks in results.

        Returns:
            JSON array of task objects.
        """
        workspace_path = Path(workspace).expanduser()
        task_store = TaskStore(workspace_path)

        try:
            # Map status filter to TaskStore status field
            status_filter = None
            if status:
                status_lower = status.lower()
                if status_lower == "pending":
                    status_filter = "pending"
                elif status_lower == "in-progress":
                    status_filter = "in-progress"
                elif status_lower == "completed":
                    # Completed tasks are checked
                    include_completed = True

            tasks = task_store.get_tasks(
                assignee=assignee,
                status=status_filter,
                include_completed=include_completed,
            )

            # Format tasks as structured JSON
            result = []
            for task in tasks:
                task_obj = {
                    "line_num": task.line_num,
                    "task_id": task.task_id,
                    "text": task.text.strip(),
                    "checked": task.checked,
                    "status": task.status,
                    "assignee": task.assignee,
                    "due_date": task.due_date,
                    "done_date": task.done_date,
                    "depends_on": task.depends_on,
                }
                result.append(task_obj)

            return json.dumps(result, indent=2)
        except Exception as exc:
            return json.dumps(
                {"error": str(exc), "tasks": []},
                indent=2,
            )
