"""An in-memory stand-in for Linear's GraphQL API, for the Linear provider's tests.

Copied from fabric's `tests/fake_linear.py` and cut to the read-only slice the board uses. FakeLinear is a
transport, `fake(query, variables) -> data`, the callable shape LinearProvider takes. It parses the document and
executes it against a small schema (the slice of Linear's the provider reads), so it is strict where Linear is:

- only the selected fields come back; a field the type doesn't have, an argument the field doesn't take, an unknown
  fragment or a filter key the fake doesn't know is an error;
- variables must be declared, required ones given, and their values must loosely fit the declared type;
- connections honour `first`/`after` (at most 250), capped further by `caps` so tests exercise paging;
- every document's complexity is estimated as Linear does (each scalar 0.1, each object 1, a connection
  multiplies its nodes by `first`, default 50) and one over MAX_COMPLEXITY is refused.
"""
import itertools
import re

DEFAULT_STATES = (("Backlog", "backlog"), ("Todo", "unstarted"), ("In Progress", "started"),
                  ("Done", "completed"), ("Canceled", "canceled"), ("Duplicate", "duplicate"))
MAX_COMPLEXITY = 10_000  # Linear's limit for a single query
DEFAULT_FIRST = 50  # Linear's page size when a connection has no `first`
MAX_FIRST = 250

SCALARS = {"String", "ID", "Float", "Int", "Boolean", "DateTime"}
CONN = ("first", "after", "filter")
# type -> field -> (type, allowed arguments). A type ending in Connection has `nodes` and `pageInfo`.
SCHEMA = {
    "Query": {"issues": ("IssueConnection", CONN + ("includeArchived",)), "issue": ("Issue", ("id",)),
              "viewer": ("User", ())},
    "Team": {"id": ("ID", ()), "key": ("String", ())},
    "WorkflowState": {"id": ("ID", ()), "name": ("String", ()), "type": ("String", ())},
    "IssueLabel": {"id": ("ID", ()), "name": ("String", ())},
    "Issue": {"id": ("ID", ()), "identifier": ("String", ()), "number": ("Float", ()), "title": ("String", ()),
              "url": ("String", ()), "description": ("String", ()), "branchName": ("String", ()),
              "subIssueSortOrder": ("Float", ()), "createdAt": ("DateTime", ()), "completedAt": ("DateTime", ()),
              "canceledAt": ("DateTime", ()), "state": ("WorkflowState", ()), "parent": ("Issue", ()),
              "team": ("Team", ()), "creator": ("User", ()), "assignee": ("User", ()), "delegate": ("User", ()),
              "labels": ("IssueLabelConnection", CONN), "children": ("IssueConnection", CONN),
              "relations": ("IssueRelationConnection", CONN),
              "inverseRelations": ("IssueRelationConnection", CONN)},
    "User": {"id": ("ID", ()), "name": ("String", ()), "displayName": ("String", ())},
    "IssueRelation": {"id": ("ID", ()), "type": ("String", ()), "issue": ("Issue", ()),
                      "relatedIssue": ("Issue", ())},
    "PageInfo": {"hasNextPage": ("Boolean", ()), "endCursor": ("String", ())},
}
for _node in ("IssueLabel", "Issue", "IssueRelation"):
    SCHEMA[_node + "Connection"] = {"nodes": ("[%s]" % _node, ()), "pageInfo": ("PageInfo", ())}


class GraphQLError(Exception):
    pass


# ---------- parsing ----------

TOKEN = re.compile(r'\s*(?:(\.\.\.)|("(?:[^"\\]|\\.)*")|(-?\d+(?:\.\d+)?)|([A-Za-z_]\w*)|(\$)|([{}()\[\]:!,=@]))')


def tokenize(doc):
    tokens, pos = [], 0
    doc = doc.rstrip()
    while pos < len(doc):
        m = TOKEN.match(doc, pos)
        if not m:
            raise GraphQLError(f"syntax error at {doc[pos:pos + 20]!r}")
        pos = m.end()
        spread, string, number, name, dollar, punct = m.groups()
        if string is not None:
            tokens.append(("str", string[1:-1]))
        elif number is not None:
            tokens.append(("num", float(number) if "." in number else int(number)))
        elif name is not None:
            tokens.append(("name", name))
        elif punct == ",":
            continue  # commas are insignificant in GraphQL
        else:
            tokens.append(("p", spread or dollar or punct))
    return tokens


class Parser:
    def __init__(self, doc):
        self.t, self.i = tokenize(doc), 0

    def peek(self, kind=None, value=None):
        if self.i >= len(self.t):
            return False
        k, v = self.t[self.i]
        return (kind is None or k == kind) and (value is None or v == value)

    def take(self, kind=None, value=None):
        if not self.peek(kind, value):
            got = self.t[self.i] if self.i < len(self.t) else "end of document"
            raise GraphQLError(f"syntax error: expected {value or kind}, got {got}")
        self.i += 1
        return self.t[self.i - 1][1]

    def document(self):
        ops, fragments = [], {}
        while self.i < len(self.t):
            word = self.take("name")
            if word == "fragment":
                name = self.take("name")
                self.take("name", "on")
                fragments[name] = (self.take("name"), self.selection())
            elif word == "query":
                name = self.take("name")
                vardefs = self.vardefs() if self.peek("p", "(") else {}
                ops.append((word, name, vardefs, self.selection()))
            else:
                raise GraphQLError(f"syntax error: unexpected {word} (the board only reads)")
        if len(ops) != 1:
            raise GraphQLError("a document needs exactly one named operation")
        return ops[0], fragments

    def vardefs(self):
        self.take("p", "(")
        out = {}
        while not self.peek("p", ")"):
            self.take("p", "$")
            name = self.take("name")
            self.take("p", ":")
            out[name] = self.type_ref()
        self.take("p", ")")
        return out

    def type_ref(self):
        if self.peek("p", "["):
            self.take("p", "[")
            inner = self.type_ref()
            self.take("p", "]")
            ref = ("list", inner)
        else:
            ref = ("named", self.take("name"))
        if self.peek("p", "!"):
            self.take("p", "!")
            return ("nonnull", ref)
        return ref

    def selection(self):
        self.take("p", "{")
        out = []
        while not self.peek("p", "}"):
            if self.peek("p", "..."):
                self.take("p", "...")
                out.append(("spread", self.take("name")))
                continue
            name = self.take("name")
            alias = None
            if self.peek("p", ":"):
                self.take("p", ":")
                alias, name = name, self.take("name")
            args = self.args() if self.peek("p", "(") else {}
            sub = self.selection() if self.peek("p", "{") else None
            out.append(("field", alias or name, name, args, sub))
        self.take("p", "}")
        if not out:
            raise GraphQLError("empty selection set")
        return out

    def args(self):
        self.take("p", "(")
        out = {}
        while not self.peek("p", ")"):
            name = self.take("name")
            self.take("p", ":")
            out[name] = self.value()
        self.take("p", ")")
        return out

    def value(self):
        if self.peek("p", "$"):
            self.take("p", "$")
            return ("var", self.take("name"))
        if self.peek("p", "{"):
            self.take("p", "{")
            obj = {}
            while not self.peek("p", "}"):
                key = self.take("name")
                self.take("p", ":")
                obj[key] = self.value()
            self.take("p", "}")
            return ("obj", obj)
        if self.peek("p", "["):
            self.take("p", "[")
            items = []
            while not self.peek("p", "]"):
                items.append(self.value())
            self.take("p", "]")
            return ("list", items)
        if self.peek("str") or self.peek("num"):
            return ("lit", self.take())
        word = self.take("name")
        return ("lit", {"true": True, "false": False, "null": None}.get(word, word))


def type_ok(value, ref):
    """Loose check of a variable value against its declared type."""
    if ref[0] == "nonnull":
        return value is not None and type_ok(value, ref[1])
    if value is None:
        return True
    if ref[0] == "list":
        return isinstance(value, list) and all(type_ok(v, ref[1]) for v in value)
    name = ref[1]
    if name in ("String", "ID", "DateTime"):
        return isinstance(value, str)
    if name == "Float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "Int":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "Boolean":
        return isinstance(value, bool)
    raise GraphQLError(f"unknown type {name}")


# ---------- the fake workspace ----------

class FakeLinear:
    def __init__(self, team="TM", states=DEFAULT_STATES, page_size=2):
        self._ids = (f"id-{n}" for n in itertools.count(1))
        # Per-connection page cap on top of `first`, small so the provider's paging is exercised.
        self.caps = {name: page_size for name in ("issues", "children", "inverseRelations", "labels")}
        self.calls = []  # (operation name, variables)
        self.complexity = {}  # operation name -> highest estimated complexity seen
        self.teams, self.labels, self.issues, self.relations = [], [], {}, []
        self.users = {}
        self.viewer = self.add_user("Kåre Blakstad", "kaare")  # the API key's user
        self.add_team(team, states)

    # ---- workspace setup ----

    def add_team(self, key, states=DEFAULT_STATES):
        team = {"id": next(self._ids), "key": key, "counter": 0,
                "states": [{"id": next(self._ids), "name": n, "type": t} for n, t in states]}
        self.teams.append(team)
        return team

    def team(self, key):
        return next(t for t in self.teams if t["key"] == key)

    def add_user(self, name, display_name):
        user = {"id": "user-" + display_name, "name": name, "displayName": display_name}
        self.users.setdefault(user["id"], user)
        return self.users[user["id"]]

    def add_issue(self, title, description="", parent=None, labels=(), state="Backlog", assignee=None,
                  delegate=None, sort_order=None, team="TM"):
        """An issue made in Linear by hand; `parent` is the parent issue, `assignee` a display name."""
        team = self.team(team)
        team["counter"] += 1
        number = team["counter"]
        issue = {"id": next(self._ids), "number": number, "title": title, "description": description,
                 "teamId": team["id"], "parentId": parent["id"] if parent else None,
                 "labelIds": [self._label(n)["id"] for n in labels],
                 "stateId": next(s["id"] for s in team["states"] if s["name"] == state),
                 "assigneeId": self.add_user(assignee.title(), assignee)["id"] if assignee else None,
                 "delegateId": self.add_user(delegate.title(), delegate)["id"] if delegate else None,
                 "creatorId": self.viewer["id"],
                 "subIssueSortOrder": sort_order if sort_order is not None else (float(number) if parent else None),
                 "createdAt": f"2026-10-14T10:{number:02d}:00.000Z",
                 "completedAt": "2026-10-15T09:00:00.000Z" if state == "Done" else None,
                 "canceledAt": "2026-10-15T09:00:00.000Z" if state in ("Canceled", "Duplicate") else None}
        self.issues[issue["id"]] = issue
        return issue

    def block(self, blocker, blocked):
        """`blocker` blocks `blocked`: a `blocks` relation from the blocker, as Linear stores it."""
        self.relations.append({"id": next(self._ids), "type": "blocks", "issueId": blocker["id"],
                               "relatedIssueId": blocked["id"]})

    def relate(self, a, b, kind="related"):
        self.relations.append({"id": next(self._ids), "type": kind, "issueId": a["id"], "relatedIssueId": b["id"]})

    def _label(self, name):
        for label in self.labels:
            if label["name"] == name:
                return label
        label = {"id": next(self._ids), "name": name}
        self.labels.append(label)
        return label

    def ops(self):
        return [name for name, _ in self.calls]

    # ---- transport ----

    def __call__(self, query, variables):
        (kind, name, vardefs, selection), fragments = Parser(query).document()
        self.calls.append((name, dict(variables)))
        for var in variables:
            if var not in vardefs:
                raise GraphQLError(f"{name}: variable ${var} is not declared")
        for var, ref in vardefs.items():
            if not type_ok(variables.get(var), ref):
                raise GraphQLError(f"{name}: variable ${var} got {variables.get(var)!r}, not a valid {ref}")
        ctx = {"vars": variables, "vardefs": vardefs, "fragments": fragments, "op": name}
        cost = self.cost_of("Query", selection, ctx)
        self.complexity[name] = max(self.complexity.get(name, 0), cost)
        if cost > MAX_COMPLEXITY:
            raise GraphQLError(f"Query too complex: {name} has complexity {cost:.0f}, the maximum is {MAX_COMPLEXITY}")
        return self._resolve("Query", None, selection, ctx)

    def estimate(self, query, variables=None):
        """The complexity Linear would charge for `query`, without running it."""
        (kind, name, vardefs, selection), fragments = Parser(query).document()
        ctx = {"vars": variables or {}, "vardefs": vardefs, "fragments": fragments, "op": name}
        return self.cost_of("Query", selection, ctx)

    # ---- execution ----

    def _fields(self, type_name, selection, ctx):
        """The selection with fragment spreads expanded, checked against the schema."""
        out = []
        for sel in selection:
            if sel[0] == "spread":
                if sel[1] not in ctx["fragments"]:
                    raise GraphQLError(f"unknown fragment {sel[1]}")
                on, sub = ctx["fragments"][sel[1]]
                if on != type_name:
                    raise GraphQLError(f"fragment {sel[1]} on {on} spread into {type_name}")
                out += self._fields(type_name, sub, ctx)
                continue
            _, alias, name, args, sub = sel
            if name not in SCHEMA[type_name]:
                raise GraphQLError(f'Cannot query field "{name}" on type "{type_name}"')
            ftype, allowed = SCHEMA[type_name][name]
            for arg in args:
                if arg not in allowed:
                    raise GraphQLError(f'Unknown argument "{arg}" on field "{type_name}.{name}"')
            inner = ftype.strip("[]")
            if inner in SCALARS and sub is not None:
                raise GraphQLError(f'Field "{name}" of type {ftype} must not have a selection')
            if inner not in SCALARS and sub is None:
                raise GraphQLError(f'Field "{name}" of type {ftype} must have a selection')
            out.append((alias, name, {k: self._value(v, ctx) for k, v in args.items()}, sub, ftype))
        return out

    def _value(self, v, ctx):
        kind, x = v
        if kind == "var":
            if x not in ctx["vardefs"]:
                raise GraphQLError(f"{ctx['op']}: variable ${x} is used but not declared")
            return ctx["vars"].get(x)
        if kind == "obj":
            return {k: self._value(i, ctx) for k, i in x.items()}
        if kind == "list":
            return [self._value(i, ctx) for i in x]
        return x

    def cost_of(self, type_name, selection, ctx):
        total = 0.0
        for _, name, args, sub, ftype in self._fields(type_name, selection, ctx):
            if ftype in SCALARS:
                total += 0.1
            elif ftype.endswith("Connection"):
                first = args.get("first") or DEFAULT_FIRST
                total += 1
                for _, cname, _, csub, ctype in self._fields(ftype, sub, ctx):
                    if cname == "nodes":
                        total += first * (1 + self.cost_of(ctype.strip("[]"), csub, ctx))
                    else:
                        total += 1 + self.cost_of(ctype, csub, ctx)
            else:
                total += 1 + self.cost_of(ftype, sub, ctx)
        return round(total, 1)

    def _resolve(self, type_name, obj, selection, ctx):
        out = {}
        for alias, name, args, sub, ftype in self._fields(type_name, selection, ctx):
            resolver = getattr(self, f"r_{type_name}_{name}", None)
            value = resolver(obj, **args) if resolver else obj[name]
            if ftype.endswith("Connection"):
                out[alias] = self._connection(ftype, name, value, args, sub, ctx)
            elif ftype in SCALARS or value is None:
                out[alias] = value
            else:
                out[alias] = self._resolve(ftype, value, sub, ctx)
        return out

    def _connection(self, ftype, name, items, args, sub, ctx):
        first = args.get("first") or DEFAULT_FIRST
        if not isinstance(first, int) or first < 1 or first > MAX_FIRST:
            raise GraphQLError(f"{name}: first must be 1..{MAX_FIRST}")
        size = min(first, self.caps.get(name) or first)
        after = args.get("after")
        start = int(after) if after else 0
        page = items[start:start + size]
        conn = {"nodes": page, "pageInfo": {"hasNextPage": start + size < len(items),
                                            "endCursor": str(start + len(page))}}
        node_type = SCHEMA[ftype]["nodes"][0].strip("[]")
        out = {}
        for alias, cname, _, csub, ctype in self._fields(ftype, sub, ctx):
            if cname == "nodes":
                out[alias] = [self._resolve(node_type, n, csub, ctx) for n in page]
            else:
                out[alias] = self._resolve(ctype, conn[cname], csub, ctx)
        return out

    # ---- filters, the slice of Linear's IssueFilter the provider sends ----

    COMPARATORS = {"eq": lambda a, b: a == b, "in": lambda a, b: a in b, "nin": lambda a, b: a not in b,
                   "contains": lambda a, b: a is not None and b in a,
                   "startsWith": lambda a, b: a is not None and a.startswith(b)}

    def _compare(self, cond, actual):
        unknown = sorted(set(cond) - set(self.COMPARATORS))
        if unknown:
            raise GraphQLError(f"comparator {', '.join(unknown)} is not supported by the fake")
        return all(self.COMPARATORS[op](actual, value) for op, value in cond.items())

    def _matches(self, issue, filt):
        known = {"team", "number", "labels", "state", "description", "parent", "or"}
        unknown = sorted(set(filt) - known)
        if unknown:
            raise GraphQLError(f"unknown filter field(s) {', '.join(unknown)}")
        for key, cond in filt.items():
            if key == "or":
                if not any(self._matches(issue, f) for f in cond):
                    return False
            elif key == "team":
                if not self._compare(cond["key"], self._team(issue)["key"]):
                    return False
            elif key == "number":
                if not self._compare(cond, issue["number"]):
                    return False
            elif key == "labels":
                if not any(self._compare(cond["name"], n) for n in self.label_names(issue)):
                    return False
            elif key == "state":
                if not self._compare(cond["type"], self.r_Issue_state(issue)["type"]):
                    return False
            elif key == "description":
                if not self._compare(cond, issue["description"]):
                    return False
            elif key == "parent":
                parent = self.r_Issue_parent(issue)
                if parent is None or not self._matches(parent, cond):
                    return False
        return True

    def label_names(self, issue):
        return [lbl["name"] for lbl in self.labels if lbl["id"] in issue["labelIds"]]

    def _team(self, issue):
        return next(t for t in self.teams if t["id"] == issue["teamId"])

    # ---- Query ----

    def r_Query_issues(self, _, filter=None, first=None, after=None, includeArchived=None):
        hits = [i for i in self.issues.values() if self._matches(i, filter or {})]
        return sorted(hits, key=lambda i: -i["number"])  # Linear lists newest first

    def r_Query_issue(self, _, id):
        if id not in self.issues:
            raise GraphQLError(f"Entity not found: issue {id}")
        return self.issues[id]

    def r_Query_viewer(self, _):
        return self.viewer

    # ---- object fields ----

    def r_Issue_identifier(self, issue):
        return f"{self._team(issue)['key']}-{issue['number']}"

    def r_Issue_url(self, issue):
        return f"https://linear.app/fabricskill/issue/{self.r_Issue_identifier(issue)}/slug"

    def r_Issue_branchName(self, issue):
        return f"kaare/{self.r_Issue_identifier(issue).lower()}-slug"

    def r_Issue_team(self, issue):
        return self._team(issue)

    def r_Issue_state(self, issue):
        return next(s for s in self._team(issue)["states"] if s["id"] == issue["stateId"])

    def r_Issue_creator(self, issue):
        return self.users.get(issue.get("creatorId"))

    def r_Issue_assignee(self, issue):
        return self.users.get(issue.get("assigneeId"))

    def r_Issue_delegate(self, issue):
        return self.users.get(issue.get("delegateId"))

    def r_Issue_parent(self, issue):
        return self.issues.get(issue["parentId"])

    def r_Issue_labels(self, issue, first=None, after=None, filter=None):
        return [lbl for lbl in self.labels if lbl["id"] in issue["labelIds"]]

    def r_Issue_children(self, issue, first=None, after=None, filter=None):
        # Linear's own order, not sub-issue order: the provider sorts.
        return sorted((i for i in self.issues.values() if i["parentId"] == issue["id"]), key=lambda i: -i["number"])

    def r_Issue_relations(self, issue, first=None, after=None, filter=None):
        return [r for r in self.relations if r["issueId"] == issue["id"]]

    def r_Issue_inverseRelations(self, issue, first=None, after=None, filter=None):
        return [r for r in self.relations if r["relatedIssueId"] == issue["id"]]

    def r_IssueRelation_issue(self, rel):
        return self.issues[rel["issueId"]]

    def r_IssueRelation_relatedIssue(self, rel):
        return self.issues[rel["relatedIssueId"]]
