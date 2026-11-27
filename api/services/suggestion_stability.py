"""Check model selectors against changed observed text, without altering saved HTML."""

from .html_extract import _containers, _isolate, _nodes, _parse


def _observations(root, fields, record):
    containers = _containers(root, record)
    tree = root.getroottree()
    observations = []
    targets = []
    if record:
        observations.append(('/record/selector', [tree.getpath(node) for node in containers]))
    for index, rule in enumerate(fields):
        relative = rule['scope'] == 'record'
        groups = []
        for context in (containers or []) if relative else [root]:
            isolated = _isolate(context) if relative else context
            mapping = dict(zip(isolated.iter(), context.iter())) if relative else {}
            matches = []
            for node in _nodes(isolated, rule, relative=relative):
                if hasattr(node, 'tag'):
                    actual = mapping.get(node, node)
                    matches.append(('element', tree.getpath(actual)))
                    targets.append((actual, 'element'))
                elif getattr(node, 'getparent', lambda: None)() is not None:
                    parent = node.getparent()
                    actual = mapping.get(parent, parent)
                    kind = 'tail' if node.is_tail else 'text' if node.is_text else 'attribute'
                    matches.append((kind, tree.getpath(actual), getattr(node, 'attrname', None)))
                    if kind != 'attribute':
                        targets.append((actual, kind))
                else:
                    # Scalar XPath formulas have no source node to perturb.
                    matches.append(('scalar', type(node).__name__))
            groups.append(matches)
        observations.append((f'/fields/{index}/selector', groups))
    return observations, targets


def unstable_suggestion_path(body, fields, record, *, charset=''):
    """Return the first rule changing its selected nodes when observed text changes.

    Tags, attributes, labels outside selected values and document order stay intact.
    This is a regression check on the supplied sample, not proof for other layouts.
    Scalar XPath formulas and attribute values are outside this text perturbation.
    """
    root = _parse(body, '', charset)
    before, targets = _observations(root, fields, record)
    for index, (node, kind) in enumerate(targets):
        marker = f'scout_changed_value_{index}'
        if kind == 'element':
            for child in node.iter():
                if child.text and child.text.strip():
                    child.text = marker
                if child is not node and child.tail and child.tail.strip():
                    child.tail = marker
        elif getattr(node, kind) and getattr(node, kind).strip():
            setattr(node, kind, marker)
    after, _ = _observations(root, fields, record)
    for (path, original), (_, changed) in zip(before, after):
        if original != changed:
            return path
    return None
