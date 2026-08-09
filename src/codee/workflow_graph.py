"""Reflex wrappers for the React Flow workflow graph."""
from typing import Any

import reflex as rx
from reflex_base.components.component import Component, NoSSRComponent, field
from reflex_base.event import EventHandler, no_args_event_spec
from reflex_base.vars.base import Var

REACT_FLOW_LIBRARY = "@xyflow/react@12.9.3"


def _edge_click_signature(
    event: Var, edge: Var
) -> tuple[Var[list[str]], Var[float], Var[float]]:
    """Pass the clicked edge's skills and the viewport click position."""
    return (
        Var(_js_expr=f"({edge}?.data?.skills ?? [])"),
        Var(_js_expr=f"{event}.clientX"),
        Var(_js_expr=f"{event}.clientY"),
    )


class ReactFlow(NoSSRComponent):
    library = REACT_FLOW_LIBRARY
    tag = "ReactFlow"

    nodes: Var[list[dict[str, Any]]] = field()
    edges: Var[list[dict[str, Any]]] = field()
    fit_view: Var[bool] = field()
    fit_view_options: Var[dict[str, Any]] = field()
    nodes_draggable: Var[bool] = field()
    nodes_connectable: Var[bool] = field()
    elements_selectable: Var[bool] = field()
    min_zoom: Var[float] = field()
    max_zoom: Var[float] = field()
    pro_options: Var[dict[str, Any]] = field()

    on_edge_click: EventHandler[_edge_click_signature] = field()
    on_pane_click: EventHandler[no_args_event_spec] = field()

    def add_imports(self) -> dict[str, str]:
        return {"": "@xyflow/react/dist/style.css"}


class Background(Component):
    library = REACT_FLOW_LIBRARY
    tag = "Background"

    gap: Var[int] = field()
    size: Var[int] = field()
    color: Var[str] = field()


def workflow_graph(
    nodes: list[dict[str, Any]] | Var,
    edges: list[dict[str, Any]] | Var,
    on_edge_click: Any = None,
    on_pane_click: Any = None,
) -> rx.Component:
    return rx.box(
        ReactFlow.create(
            Background.create(gap=20, size=1, color="var(--codee-grid)"),
            nodes=nodes,
            edges=edges,
            on_edge_click=on_edge_click,
            on_pane_click=on_pane_click,
            fit_view=True,
            fit_view_options={"padding": 0.25},
            nodes_draggable=False,
            nodes_connectable=False,
            elements_selectable=True,
            min_zoom=0.25,
            max_zoom=1.75,
            pro_options={"hideAttribution": True},
            width="100%",
            height="100%",
        ),
        height="560px",
        min_height="28rem",
        background="var(--codee-surface)",
        border="1px solid var(--codee-border)",
        width="100%",
    )
