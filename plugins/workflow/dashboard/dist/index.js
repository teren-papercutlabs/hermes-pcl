(function () {
  "use strict";

  var sdk = window.__HERMES_PLUGIN_SDK__;
  if (!sdk || !window.__HERMES_PLUGINS__) return;

  var React = sdk.React;
  var h = React.createElement;
  var hooks = sdk.hooks;
  var components = sdk.components || {};
  var flow = sdk.flow || {};
  var Card = components.Card;
  var CardContent = components.CardContent;
  var Badge = components.Badge;
  var Button = components.Button;
  var Input = components.Input;
  var Label = components.Label;
  var Select = components.Select;
  var SelectOption = components.SelectOption;
  var Tabs = components.Tabs;
  var TabsList = components.TabsList;
  var TabsTrigger = components.TabsTrigger;
  var API = "/api/plugins/workflow";
  var BADGE_LABELS = {
    parked: "Parked",
    pending_approval: "Pending approval",
    needs_review: "Needs review",
    exception: "Exception",
  };

  function asArray(value) {
    return Array.isArray(value) ? value : [];
  }

  function listify(value) {
    return value === undefined || value === null ? [] : (Array.isArray(value) ? value : [value]);
  }

  function first(value, fallback) {
    return value === undefined || value === null || value === "" ? fallback : value;
  }

  function templateKey(template) {
    var id = String(first(template.template_id, first(template.id, first(template.slug, "workflow"))));
    var version = first(template.version, first(template.template_version, null));
    return version === null || id.indexOf("@") >= 0 ? id : id + "@" + version;
  }

  function templateLabel(template) {
    return String(first(template.label, first(template.name, first(template.template_name, templateKey(template)))));
  }

  function templatesFrom(board) {
    return asArray(first(board && board.templates, first(board && board.template_versions, []))).map(function (item) {
      var spec = item.spec || item.workflow || {};
      var steps = asArray(first(item.steps, first(spec.steps, [])));
      return Object.assign({}, item, { steps: steps, _key: templateKey(item), _label: templateLabel(item) });
    });
  }

  function instancesFrom(board) {
    var instances = first(board && board.instances, first(board && board.cards, []));
    if (Array.isArray(instances)) return instances;
    if (!instances || typeof instances !== "object") return [];
    return Object.keys(instances).reduce(function (all, key) {
      return all.concat(asArray(instances[key]));
    }, []);
  }

  function instanceTemplateKey(instance) {
    if (instance.template_key) return String(instance.template_key);
    var id = first(instance.template_id, first(instance.template_slug, first(instance.workflow_id, "workflow")));
    var version = first(instance.template_version, first(instance.version, null));
    return version === null || String(id).indexOf("@") >= 0 ? String(id) : String(id) + "@" + version;
  }

  function stepKey(step) {
    return String(first(step.key, first(step.step_key, first(step.id, first(step.name, "stage")))));
  }

  function stepLabel(step) {
    return String(first(step.label, first(step.name, first(step.title, stepKey(step)))));
  }

  function stageSteps(template) {
    return asArray(template && template.steps).map(function (step, index) {
      return Object.assign({}, step, { _key: stepKey(step), _label: stepLabel(step), _index: index });
    });
  }

  function currentStep(instance) {
    return String(first(instance.current_step_key, first(instance.current_step, first(instance.step_key, first(instance.stage_key, "unknown")))));
  }

  function stageStartedAt(instance) {
    return first(instance.parked_since, first(instance.stage_started_at, first(instance.stage_entered_at, first(instance.time_in_stage_since, first(instance.entered_at, null)))));
  }

  function durationLabel(seconds) {
    var total = Math.max(0, Number(seconds));
    if (!isFinite(total)) return "";
    if (total < 60) return Math.floor(total) + "s";
    if (total < 3600) return Math.floor(total / 60) + "m";
    if (total < 86400) return Math.floor(total / 3600) + "h";
    return Math.floor(total / 86400) + "d";
  }

  function relativeStageTime(instance) {
    if (instance.time_in_stage !== undefined && typeof instance.time_in_stage === "string") return instance.time_in_stage;
    if (instance.time_in_stage !== undefined && typeof instance.time_in_stage === "number") return durationLabel(instance.time_in_stage);
    if (instance.time_in_stage_seconds !== undefined) return durationLabel(instance.time_in_stage_seconds);
    var started = stageStartedAt(instance);
    if (!started) return "time unavailable";
    if (sdk.utils && sdk.utils.isoTimeAgo) return sdk.utils.isoTimeAgo(started);
    return "in stage";
  }

  function exactTime(instance) {
    var started = stageStartedAt(instance);
    return started ? new Date(started).toISOString() : "Stage entry time unavailable";
  }

  function badgeKeys(instance) {
    var values = asArray(instance.badges);
    if (!values.length) {
      values = [instance.state, instance.status, instance.review_state, instance.approval_state];
    }
    return values.map(function (value) {
      return String(value || "").toLowerCase().replace(/[ -]/g, "_");
    }).filter(function (value, index, all) {
      return !!BADGE_LABELS[value] && all.indexOf(value) === index;
    });
  }

  function badgeTone(key) {
    return key === "exception" ? "destructive" : key === "needs_review" ? "warning" : "secondary";
  }

  function stageCounts(board, template, instances) {
    var source = first(board && board.stage_counts, first(board && board.counts, first(board && board.per_stage_counts, {})));
    var result = {};
    if (Array.isArray(source)) {
      source.forEach(function (item) {
        var key = first(item.step_key, first(item.stage_key, first(item.key, null)));
        if (key !== null) result[String(key)] = Number(first(item.count, first(item.instance_count, 0)));
      });
    } else if (source && typeof source === "object") {
      var selected = source[templateKey(template)] || source[template.template_id] || source;
      if (selected && typeof selected === "object") {
        Object.keys(selected).forEach(function (key) {
          var value = selected[key];
          result[key] = Number(typeof value === "object" ? first(value.count, first(value.instance_count, 0)) : value);
        });
      }
    }
    var scoped = instances.filter(function (item) { return instanceTemplateKey(item) === templateKey(template); });
    stageSteps(template).forEach(function (step) {
      if (result[step._key] === undefined) {
        result[step._key] = scoped.filter(function (item) { return currentStep(item) === step._key; }).length;
      }
    });
    return result;
  }

  function actionList(actions, keyNames) {
    for (var i = 0; i < keyNames.length; i += 1) {
      if (Array.isArray(actions && actions[keyNames[i]])) return actions[keyNames[i]];
    }
    return [];
  }

  function safeError(error) {
    return error && error.message ? error.message : "The workflow data could not be loaded.";
  }

  function Skeleton() {
    return h("div", { className: "hermes-workflow-skeleton", "aria-label": "Loading workflow" },
      [1, 2, 3].map(function (column) {
        return h("div", { className: "hermes-workflow-skeleton-column", key: column },
          h("div", { className: "hermes-workflow-skeleton-line wide" }),
          h("div", { className: "hermes-workflow-skeleton-card" }),
          h("div", { className: "hermes-workflow-skeleton-card short" }));
      }));
  }

  function EmptyState(props) {
    return h("div", { className: "hermes-workflow-empty" },
      h("p", { className: "hermes-workflow-empty-title" }, props.title),
      props.detail ? h("p", { className: "hermes-workflow-muted" }, props.detail) : null);
  }

  function WorkflowCard(props) {
    var instance = props.instance;
    var badges = badgeKeys(instance);
    var entity = String(first(instance.entity_key, first(instance.entity_ref, first(instance.entity_id, instance.task_id))));
    return h(Card, {
      className: "hermes-workflow-card",
      role: "button",
      tabIndex: 0,
      onClick: function () { props.onOpen(instance.task_id); },
      onKeyDown: function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          props.onOpen(instance.task_id);
        }
      },
      "aria-label": "Open workflow instance " + entity,
    }, h(CardContent, { className: "hermes-workflow-card-content" },
      h("div", { className: "hermes-workflow-card-ref" }, entity),
      h("div", { className: "hermes-workflow-card-meta", title: exactTime(instance) },
        h("span", null, relativeStageTime(instance)),
        h("span", { className: "hermes-workflow-card-state" }, String(first(instance.state, first(instance.status, "active"))))),
      badges.length ? h("div", { className: "hermes-workflow-badges" }, badges.map(function (key) {
        return h(Badge, { key: key, variant: badgeTone(key) }, BADGE_LABELS[key]);
      })) : null));
  }

  function StageColumn(props) {
    return h("section", { className: "hermes-workflow-column", role: "listitem", "aria-label": props.stage._label },
      h("div", { className: "hermes-workflow-column-header" },
        h("div", null, h("h3", null, props.stage._label),
          h("span", { className: "hermes-workflow-column-key" }, props.stage._key)),
        h("span", { className: "hermes-workflow-count" }, String(props.count))),
      h("div", { className: "hermes-workflow-column-body" }, props.cards.length ? props.cards.map(function (instance) {
        return h(WorkflowCard, { key: instance.task_id, instance: instance, onOpen: props.onOpen });
      }) : h("p", { className: "hermes-workflow-column-empty" }, "No active instances")));
  }

  function BoardView(props) {
    var stages = stageSteps(props.template);
    var cards = props.instances.filter(function (instance) { return instanceTemplateKey(instance) === templateKey(props.template); });
    var counts = stageCounts(props.board, props.template, props.allInstances);
    if (!stages.length) return h(EmptyState, { title: "No stages in this template.", detail: "The template has no published stage definition." });
    return h("div", { className: "hermes-workflow-board" },
      h("div", { className: "hermes-workflow-columns", role: "list" }, stages.map(function (stage) {
        return h(StageColumn, {
          key: stage._key,
          stage: stage,
          count: counts[stage._key] || 0,
          cards: cards.filter(function (instance) { return currentStep(instance) === stage._key; }),
          onOpen: props.onOpen,
        });
      })));
  }

  function graphEdges(steps) {
    var edges = [];
    function add(source, target, label) {
      if (!source || !target || source === target) return;
      var id = String(source) + "->" + String(target) + ":" + String(label || "");
      if (!edges.some(function (edge) { return edge.id === id; })) {
        edges.push({ id: id, source: String(source), target: String(target), label: label || undefined, type: "smoothstep", animated: false });
      }
    }
    steps.forEach(function (step) {
      var advance = first(step.advance_to, first(step.advanceTo, null));
      listify(advance).forEach(function (target) {
        add(step._key, typeof target === "object" ? first(target.step_key, first(target.key, target.target)) : target, "advance");
      });
      listify(first(step.waits, first(step.wait, []))).forEach(function (wait) {
        var target = first(wait.advance_to, first(wait.advanceTo, first(wait.target_step_key, first(wait.step_key, wait.target))));
        add(step._key, typeof target === "object" ? first(target.step_key, first(target.key, target.target)) : target, "wait");
      });
    });
    return edges;
  }

  function StageNode(props) {
    var Handle = flow.Handle;
    var Position = flow.Position || {};
    return h("div", { className: "hermes-workflow-node" },
      Handle ? h(Handle, { type: "target", position: Position.Left || "left", isConnectable: false }) : null,
      h("div", { className: "hermes-workflow-node-label" }, props.data.label),
      h("div", { className: "hermes-workflow-node-count" }, String(props.data.count) + " active"),
      Handle ? h(Handle, { type: "source", position: Position.Right || "right", isConnectable: false }) : null);
  }

  function GraphView(props) {
    var stages = stageSteps(props.template);
    var counts = stageCounts(props.board, props.template, props.allInstances);
    var nodes = stages.map(function (stage) {
      return {
        id: stage._key,
        type: "workflowStage",
        position: { x: stage._index * 250, y: 100 + (stage._index % 2) * 45 },
        data: { label: stage._label, count: counts[stage._key] || 0 },
        draggable: false,
        selectable: false,
      };
    });
    if (!flow.ReactFlow) return h(EmptyState, { title: "Stage graph unavailable.", detail: "The dashboard host did not provide the graph renderer." });
    var graph = h(flow.ReactFlow, {
      nodes: nodes,
      edges: graphEdges(stages),
      nodeTypes: { workflowStage: StageNode },
      fitView: true,
      fitViewOptions: { padding: 0.2 },
      nodesDraggable: false,
      nodesConnectable: false,
      elementsSelectable: false,
      zoomOnDoubleClick: false,
      proOptions: { hideAttribution: true },
      className: "hermes-workflow-flow",
    }, flow.Controls ? h(flow.Controls, { showInteractive: false }) : null,
      flow.Background ? h(flow.Background, { gap: 24, size: 1, color: "var(--muted-foreground)" }) : null);
    return h("div", { className: "hermes-workflow-graph", "aria-label": "Workflow stage graph" },
      flow.ReactFlowProvider ? h(flow.ReactFlowProvider, null, graph) : graph);
  }

  function TimelineSkeleton() {
    return h("div", { className: "hermes-workflow-timeline-skeleton" },
      h("div", { className: "hermes-workflow-skeleton-line wide" }),
      h("div", { className: "hermes-workflow-skeleton-line" }),
      h("div", { className: "hermes-workflow-skeleton-line" }),
      h("div", { className: "hermes-workflow-skeleton-line short" }));
  }

  function TimelineRow(props) {
    var row = props.row || {};
    var timestamp = first(row.created_at, first(row.occurred_at, row.timestamp));
    return h("li", { className: "hermes-workflow-timeline-row" },
      h("div", { className: "hermes-workflow-timeline-dot" }),
      h("div", { className: "hermes-workflow-timeline-body" },
        h("div", { className: "hermes-workflow-timeline-heading" },
          h("strong", null, String(first(row.to_step, first(row.step_key, first(row.state, "Transition"))))),
          timestamp ? h("time", { dateTime: timestamp, title: new Date(timestamp).toISOString() }, sdk.utils && sdk.utils.isoTimeAgo ? sdk.utils.isoTimeAgo(timestamp) : timestamp) : null),
        h("p", { className: "hermes-workflow-muted" }, String(first(row.summary, first(row.event_type, first(row.from_step ? row.from_step + " to " + row.to_step : "State changed", "State changed")))))));
  }

  function DetailDrawer(props) {
    var identity = props.timeline && (props.timeline.identity || props.timeline.instance || props.timeline);
    var rows = asArray(props.timeline && (props.timeline.transitions || props.timeline.timeline || props.timeline.events));
    return h("aside", { className: "hermes-workflow-detail", "aria-label": "Workflow instance details" },
      h("div", { className: "hermes-workflow-detail-header" },
        h(Button, { variant: "ghost", size: "sm", onClick: props.onClose }, "Back"),
        h(Button, { variant: "ghost", size: "sm", onClick: props.onClose, "aria-label": "Close details" }, "Close")),
      props.loading ? h(TimelineSkeleton) : props.error ? h("div", { className: "hermes-workflow-error" }, props.error) : !props.timeline ? h(EmptyState, { title: "No detail available." }) :
        h("div", { className: "hermes-workflow-detail-body" },
          h("div", { className: "hermes-workflow-identity" },
            h("p", { className: "hermes-workflow-eyebrow" }, "Workflow instance"),
            h("h2", null, String(first(identity && identity.entity_key, first(identity && identity.entity_ref, props.taskId)))),
            h("dl", null,
              h("div", null, h("dt", null, "Task"), h("dd", null, props.taskId)),
              h("div", null, h("dt", null, "State"), h("dd", null, String(first(identity && identity.state, first(identity && identity.status, "unknown"))))),
              h("div", null, h("dt", null, "Current stage"), h("dd", null, String(first(identity && identity.current_step_key, first(identity && identity.current_step, "unknown"))))))),
          h("div", { className: "hermes-workflow-timeline" },
            h("h3", null, "Transition timeline"),
            rows.length ? h("ol", null, rows.map(function (row, index) { return h(TimelineRow, { key: row.id || index, row: row }); })) : h(EmptyState, { title: "No transitions recorded.", detail: "This instance has no timeline events yet." }))));
  }

  function ReviewAction(props) {
    var item = props.item;
    var eventId = first(item.event_id, first(item.id, ""));
    var entity = first(item.entity_key, first(item.entity_ref, first(item.task_id, "Unknown entity")));
    var candidates = asArray(first(item.candidates, first(item.candidate_summaries, [])));
    return h("div", { className: "hermes-workflow-action-card" },
      h("div", { className: "hermes-workflow-action-header" },
        h("div", null, h("strong", null, "Needs review"), h("p", { className: "hermes-workflow-muted" }, String(entity))),
        h(Badge, { variant: "warning" }, "Review")),
      item.summary ? h("p", { className: "hermes-workflow-action-summary" }, String(item.summary)) : null,
      candidates.length ? h("div", { className: "hermes-workflow-candidates" }, candidates.map(function (candidate, index) {
        var taskId = first(candidate.task_id, first(candidate.id, null));
        return h("div", { className: "hermes-workflow-candidate", key: taskId || index },
          h("span", null, String(first(candidate.label, first(candidate.entity_key, first(candidate.summary, taskId || "Candidate"))))),
          h(Button, { size: "sm", disabled: props.busy, onClick: function () { props.onResolve(eventId, taskId); } }, "Choose"));
      })) : null,
      h("div", { className: "hermes-workflow-action-footer" },
        h(Button, { variant: "outline", size: "sm", disabled: props.busy, onClick: function () { props.onResolve(eventId, null); } }, "Neither")));
  }

  function parseObject(value) {
    try {
      var parsed = JSON.parse(value);
      if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") return { error: "Edit value must be a JSON object." };
      return { value: parsed };
    } catch (error) {
      return { error: "Edit value is not valid JSON." };
    }
  }

  function ApprovalAction(props) {
    var item = props.item;
    var approvalId = first(item.approval_id, first(item.id, ""));
    var identityLabel = String(first(item.entity_key, first(item.entity_ref, first(item.task_id, "Workflow action"))));
    var initial = first(item.payload, first(item.proposed_payload, {}));
    var [editing, setEditing] = hooks.useState(false);
    var [payload, setPayload] = hooks.useState(JSON.stringify(initial || {}, null, 2));
    var [token, setToken] = hooks.useState("");
    var [error, setError] = hooks.useState("");
    var submit = function (decision) {
      setError("");
      var body = { decision: decision, decided_by: "dashboard", token: token };
      if (!token.trim()) { setError("Authorization is required."); return; }
      if (decision === "edited_approved") {
        var parsed = parseObject(payload);
        if (parsed.error) { setError(parsed.error); return; }
        body.payload = parsed.value;
      }
      props.onSubmit(approvalId, body).catch(function (submitError) { setError(safeError(submitError)); });
    };
    return h("div", { className: "hermes-workflow-action-card" },
      h("div", { className: "hermes-workflow-action-header" },
        h("div", null,
          h("strong", null, "Pending approval"),
          h("p", { className: "hermes-workflow-muted" }, identityLabel)),
        h(Badge, { variant: "secondary" }, "Approval")),
      item.summary ? h("p", { className: "hermes-workflow-action-summary" }, String(item.summary)) : null,
      editing ? h("div", { className: "hermes-workflow-editor" },
        h(Label, { htmlFor: "workflow-payload-" + approvalId }, "Edited object"),
        h("textarea", { id: "workflow-payload-" + approvalId, value: payload, onChange: function (event) { setPayload(event.target.value); }, rows: 7, spellCheck: false, className: "hermes-workflow-json" })) : null,
      h("div", { className: "hermes-workflow-token-field" },
        h(Label, { htmlFor: "workflow-authorization-" + approvalId }, "Authorization"),
        h(Input, { id: "workflow-authorization-" + approvalId, type: "password", value: token, onChange: function (event) { setToken(event.target.value); }, autoComplete: "off" })),
      error ? h("p", { className: "hermes-workflow-field-error", role: "alert" }, error) : null,
      h("div", { className: "hermes-workflow-action-footer" },
        h(Button, { size: "sm", disabled: props.busy, onClick: function () { submit("approved"); } }, "Approve"),
        h(Button, { variant: "outline", size: "sm", disabled: props.busy, onClick: function () { if (!editing) setEditing(true); else submit("edited_approved"); } }, editing ? "Submit edit" : "Edit"),
        h(Button, { variant: "destructive", size: "sm", disabled: props.busy, onClick: function () { submit("rejected"); } }, "Reject")));
  }

  function ActionQueue(props) {
    var approvals = actionList(props.actions, ["approvals", "pending_approvals"]);
    var reviews = actionList(props.actions, ["needs_review", "review", "ambiguous", "events"]);
    var [busy, setBusy] = hooks.useState(false);
    var [error, setError] = hooks.useState("");
    var refresh = function (work) {
      setBusy(true); setError("");
      return work().then(function () { return props.onChanged(); }).catch(function (err) { setError(safeError(err)); }).finally(function () { setBusy(false); });
    };
    var resolve = function (eventId, taskId) {
      return refresh(function () { return sdk.fetchJSON(API + "/action/events/" + encodeURIComponent(eventId) + "/resolve", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ task_id: taskId, decided_by: "dashboard" }),
      }); });
    };
    var submit = function (approvalId, body) {
      return refresh(function () { return sdk.fetchJSON(API + "/action/approvals/" + encodeURIComponent(approvalId), {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      }); });
    };
    if (!approvals.length && !reviews.length) return h("section", { className: "hermes-workflow-actions" },
      h("div", { className: "hermes-workflow-section-heading" }, h("h2", null, "Action queue")),
      h(EmptyState, { title: "Nothing needs attention.", detail: "Review and approval actions will appear here." }));
    return h("section", { className: "hermes-workflow-actions" },
      h("div", { className: "hermes-workflow-section-heading" }, h("h2", null, "Action queue"), h("span", { className: "hermes-workflow-muted" }, String(approvals.length + reviews.length) + " open")),
      error ? h("p", { className: "hermes-workflow-error", role: "alert" }, error) : null,
      h("div", { className: "hermes-workflow-action-grid" },
        reviews.map(function (item, index) { return h(ReviewAction, { key: item.event_id || item.id || index, item: item, busy: busy, onResolve: resolve }); }),
        approvals.map(function (item, index) { return h(ApprovalAction, { key: item.approval_id || item.id || index, item: item, busy: busy, onSubmit: submit }); })));
  }

  function TemplatePicker(props) {
    if (Select && SelectOption) return h(Select, { value: props.value, onValueChange: props.onChange, "aria-label": "Workflow template" }, props.templates.map(function (template) {
      return h(SelectOption, { key: template._key, value: template._key }, template._label);
    }));
    return h("select", { value: props.value, onChange: function (event) { props.onChange(event.target.value); }, "aria-label": "Workflow template", className: "hermes-workflow-native-select" }, props.templates.map(function (template) {
      return h("option", { key: template._key, value: template._key }, template._label);
    }));
  }

  function WorkflowPage() {
    var [board, setBoard] = hooks.useState(null);
    var [actions, setActions] = hooks.useState(null);
    var [loading, setLoading] = hooks.useState(true);
    var [error, setError] = hooks.useState("");
    var [view, setView] = hooks.useState("board");
    var [selectedTemplate, setSelectedTemplate] = hooks.useState("");
    var [taskId, setTaskId] = hooks.useState("");
    var [timeline, setTimeline] = hooks.useState(null);
    var [timelineLoading, setTimelineLoading] = hooks.useState(false);
    var [timelineError, setTimelineError] = hooks.useState("");
    var templates = templatesFrom(board || {});
    var instances = instancesFrom(board || {});
    var template = templates.find(function (item) { return item._key === selectedTemplate; }) || templates[0];
    var loadData = hooks.useCallback(function () {
      setLoading(true); setError("");
      return Promise.all([sdk.fetchJSON(API + "/board"), sdk.fetchJSON(API + "/actions")]).then(function (results) {
        setBoard(results[0]); setActions(results[1]);
        var nextTemplates = templatesFrom(results[0] || {});
        setSelectedTemplate(function (current) { return nextTemplates.some(function (item) { return item._key === current; }) ? current : (nextTemplates[0] ? nextTemplates[0]._key : ""); });
      }).catch(function (err) { setError(safeError(err)); }).finally(function () { setLoading(false); });
    }, []);
    hooks.useEffect(function () { loadData(); }, [loadData]);
    var openDetail = function (id) {
      setTaskId(String(id)); setTimeline(null); setTimelineError(""); setTimelineLoading(true);
      sdk.fetchJSON(API + "/instances/" + encodeURIComponent(id) + "/timeline").then(setTimeline).catch(function (err) { setTimelineError(safeError(err)); }).finally(function () { setTimelineLoading(false); });
    };
    var closeDetail = function () { setTaskId(""); setTimeline(null); setTimelineError(""); };
    return h("main", { className: "hermes-workflow" },
      h("header", { className: "hermes-workflow-header" },
        h("div", null, h("p", { className: "hermes-workflow-eyebrow" }, "Operations"), h("h1", null, "Workflow"), h("p", { className: "hermes-workflow-muted" }, "Track live instances and resolve the next action.")),
        h(Button, { variant: "outline", size: "sm", onClick: loadData, disabled: loading }, "Refresh")),
      h("div", { className: "hermes-workflow-toolbar" },
        h("div", { className: "hermes-workflow-view-switch", role: "tablist", "aria-label": "Workflow view" },
          h(Button, { variant: view === "board" ? "default" : "ghost", size: "sm", role: "tab", "aria-selected": view === "board", onClick: function () { setView("board"); } }, "Board"),
          h(Button, { variant: view === "graph" ? "default" : "ghost", size: "sm", role: "tab", "aria-selected": view === "graph", onClick: function () { setView("graph"); } }, "Graph")),
        templates.length ? h(TemplatePicker, { templates: templates, value: template ? template._key : "", onChange: setSelectedTemplate }) : null),
      error ? h("div", { className: "hermes-workflow-error", role: "alert" }, error) : null,
      loading ? h(Skeleton) : !template ? h(EmptyState, { title: "No workflow templates available.", detail: "Published templates will appear here when the board has data." }) :
        view === "graph" ? h(GraphView, { template: template, board: board, allInstances: instances }) : h(BoardView, { template: template, board: board, instances: instances, allInstances: instances, onOpen: openDetail }),
      !loading && template ? h(ActionQueue, { actions: actions || {}, onChanged: loadData }) : null,
      taskId ? h(DetailDrawer, { taskId: taskId, timeline: timeline, loading: timelineLoading, error: timelineError, onClose: closeDetail }) : null);
  }

  window.__HERMES_PLUGINS__.register("workflow", WorkflowPage);
})();
