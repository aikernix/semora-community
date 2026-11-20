from __future__ import annotations
from typing import Any, TypedDict
from langgraph.graph import START, END, StateGraph


class AgentState(TypedDict, total=False):
    run_id: str
    settings: dict[str, Any]
    ontology_model: dict[str, Any]
    auto_align: bool
    alignment_initial_draft: dict[str, Any]
    task_superseded: bool


def build_sql_generation_graph(
    *,
    retrieve,
    analyse,
    align,
    compile_sql,
    validate_sql,
    deliver,
    execute_dependencies,
    route_after_step,
    checkpointer=None,
):
    graph = StateGraph(AgentState)
    nodes = {
        "retrieve": retrieve,
        "analyse": analyse,
        "align": align,
        "compile": compile_sql,
        "validate_sql": validate_sql,
        "execute_dependencies": execute_dependencies,
        "deliver_sql": deliver,
    }
    for name, node in nodes.items():
        graph.add_node(name, node)
    names = list(nodes)
    graph.add_edge(START, names[0])
    for current, following in zip(names, names[1:]):
        graph.add_conditional_edges(current, route_after_step, {"continue": following, "end": END})
    graph.add_edge(names[-1], END)
    return graph.compile(checkpointer=checkpointer)
