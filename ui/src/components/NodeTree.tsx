/**
 * The node tree, in the order the expert wrote it.
 *
 * Two requirements from `docs/06`, and neither is cosmetic.
 *
 * **Declaration order, not alphabetical.** The spec is explicit: the surface
 * "must show the tree the expert wrote, in the order they wrote it, not an
 * alphabetised or normalised rendering", and it notes that `CaseGenerator`
 * already respects this so that the review sheet's column order matches the
 * ruleset. A tree sorted by id would put `acquired_interest` above
 * `acquisition_is_of_a_kind_the_Act_covers` for no reason a lawyer would
 * recognise, and the surface would stop being a rendering of their artefact.
 *
 * **An exception is a defeater, not a conjunct.** This is the defect the surface
 * exists to make findable: a rule that is *structurally* valid and *legally*
 * wrong, where the "and" should have been an "or" -- or, more precisely here,
 * where a defeater has been encoded as a sibling conjunct. Rendering the
 * children as one undifferentiated list is exactly the flattening that hides it,
 * so an `exception` child is pulled out of the conjunct list and shown under a
 * heading that says what it does: it does not have to be true, and if it is, it
 * defeats the conclusion.
 */

import type { NodeKind, NodeView } from "../types";
import { Badge, Mark } from "./bits";

interface Tree {
  byId: Map<string, NodeView>;
  /** Children in declaration order, per node id. */
  childrenOf: Map<string, string[]>;
}

export function buildTree(nodes: NodeView[]): Tree {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const childrenOf = new Map<string, string[]>();
  for (const node of nodes) {
    // `children` is a list of node ids as declared. Filtered against what is
    // actually loaded so a dangling id renders as a warning rather than as a
    // blank row -- a node referring to a child that does not exist is a defect
    // `validate_ruleset` reports, and the tree must not hide it.
    childrenOf.set(node.id, node.children.filter((id) => byId.has(id)));
  }
  return { byId, childrenOf };
}

/** Children split, in declaration order within each group. */
export function splitChildren(tree: Tree, node: NodeView) {
  const ids = tree.childrenOf.get(node.id) ?? [];
  const conjuncts: NodeView[] = [];
  const defeaters: NodeView[] = [];
  for (const id of ids) {
    const child = tree.byId.get(id);
    if (!child) continue;
    if (child.kind === "exception") defeaters.push(child);
    else conjuncts.push(child);
  }
  return { conjuncts, defeaters };
}

const KIND_SAY: Record<NodeKind, string> = {
  all_of: "every conjunct must hold",
  any_of: "at least one limb must hold",
  leaf: "tested against the facts",
  exception: "defeats the conclusion if it fires",
  delegate_bn: "answered by the credence network, not by this tree",
};

export function NodeTree({
  nodes,
  selected,
  onSelect,
  flaggedNodeIds,
}: {
  nodes: NodeView[];
  selected: string;
  onSelect: (id: string) => void;
  /** Nodes carrying an unresolved or unanchored reference. */
  flaggedNodeIds: Set<string>;
}) {
  const tree = buildTree(nodes);
  const roots = nodes.filter((node) => !node.exception_of);
  const root = roots[0];
  if (!root) return <p className="empty">No nodes.</p>;
  return (
    <ul className="tree">
      <Branch
        tree={tree}
        node={root}
        depth={0}
        selected={selected}
        onSelect={onSelect}
        flaggedNodeIds={flaggedNodeIds}
      />
    </ul>
  );
}

function Branch({
  tree,
  node,
  depth,
  selected,
  onSelect,
  flaggedNodeIds,
}: {
  tree: Tree;
  node: NodeView;
  depth: number;
  selected: string;
  onSelect: (id: string) => void;
  flaggedNodeIds: Set<string>;
}) {
  const { conjuncts, defeaters } = splitChildren(tree, node);
  const flagged = flaggedNodeIds.has(node.id);
  return (
    <li className="tree-node">
      <button
        type="button"
        className="tree-button"
        aria-current={selected === node.id}
        onClick={() => onSelect(node.id)}
      >
        {node.label}
        {flagged ? (
          <>
            {" "}
            <Badge tone="stop" title="an unresolved or unanchored reference">
              !
            </Badge>
          </>
        ) : null}
        <span className="id">
          {node.id}
          <span className="kind">
            {node.kind} · {KIND_SAY[node.kind]}
          </span>
        </span>
      </button>

      {conjuncts.length > 0 ? (
        <>
          {node.kind === "all_of" ? (
            <p className="tree-branch-label">all of — every one must hold</p>
          ) : node.kind === "any_of" ? (
            <p className="tree-branch-label">any of — one is enough</p>
          ) : null}
          <ul>
            {conjuncts.map((child) => (
              <Branch
                key={child.id}
                tree={tree}
                node={child}
                depth={depth + 1}
                selected={selected}
                onSelect={onSelect}
                flaggedNodeIds={flaggedNodeIds}
              />
            ))}
          </ul>
        </>
      ) : null}

      {defeaters.length > 0 ? (
        <div className="defeaters">
          <p className="defeaters-say">
            <Mark tone="blocked">!</Mark> <strong>Defeaters.</strong> None of these needs to
            be true for <span className="mono">{node.id}</span> to hold. Any one of them,
            if it fires, defeats the conclusion — however completely the conjuncts above
            are made out.
          </p>
          <ul>
            {defeaters.map((child) => (
              <Branch
                key={child.id}
                tree={tree}
                node={child}
                depth={depth + 1}
                selected={selected}
                onSelect={onSelect}
                flaggedNodeIds={flaggedNodeIds}
              />
            ))}
          </ul>
        </div>
      ) : null}
    </li>
  );
}
