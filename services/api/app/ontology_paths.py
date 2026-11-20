"""Context expansion over published topology; never plans or rewrites SQL."""

from collections import deque


def connect_objects(required, relations):
    if len(required) < 2:
        return []
    adjacency = {}
    for relation in relations:
        for source, target in [
            (relation["from"], relation["to"]),
            (relation["to"], relation["from"]),
        ]:
            adjacency.setdefault(source, []).append((target, relation["name"]))
    connected = {sorted(required)[0]}
    result = []
    for target in sorted(required - connected):
        queue = deque((name, []) for name in sorted(connected))
        visited = set(connected)
        while queue:
            current, path = queue.popleft()
            if current == target:
                for node, relation in path:
                    connected.add(node)
                    if relation not in result:
                        result.append(relation)
                break
            for node, relation in adjacency.get(current, []):
                if node not in visited:
                    visited.add(node)
                    queue.append((node, [*path, (node, relation)]))
        else:
            raise ValueError("相关对象之间缺少可连通的已发布关系")
    return result
