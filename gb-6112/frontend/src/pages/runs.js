import { api } from "../api/workbench.js";
import { errorText } from "../components/common/fields.js";
import { empty, pageHeading, section } from "../components/common/panel.js";
import { status } from "../components/common/status.js";
import { table } from "../components/common/table.js";
import { poll } from "../hooks/poll.js";
import { el } from "../utils/dom.js";
import { time } from "../utils/format.js";

function cancelPanel(run, user, onSettled) {
  if (user.role !== "operator" || run.status !== "queued") return null;
  const issue = errorText();
  const reason = el("input", {
    name: "reason", type: "text", maxlength: "320", placeholder: "撤单原因（可选）", "aria-label": "撤单原因",
  });
  const button = el("button", {type: "button", class: "cancel-button"}, "撤销本批");
  async function confirmCancel() {
    button.disabled = true;
    issue.textContent = "";
    try {
      const settled = await api.cancelRun(run.id, {reason: reason.value || null});
      onSettled(settled);
    } catch (error) {
      // The receipt may have been lost even when an error is shown; refresh to reveal the truth.
      issue.textContent = error.message;
      button.disabled = false;
    }
  }
  button.addEventListener("click", confirmCancel);
  return el("div", {class: "cancel-panel", "data-testid": "cancel-panel"},
    el("p", {class: "cancel-hint"}, "选错卷可在交接前撤销排队或正在试写的本批，释放的占用不影响同卷其他批次。"),
    reason, button, issue);
}

function runMessage(run) {
  if (run.status === "queued") return "后台正在处理，页面会自动更新。";
  if (run.status === "cancelled") {
    return run.cancel_reason ? `批次已撤销：${run.cancel_reason}` : "批次已撤销，不再出交接单。";
  }
  return run.failure || run.result?.summary;
}

function reportState(run, user, onSettled) {
  return el("div", {class: "stack", id: "run-state"}, status(run.status),
    el("p", {}, runMessage(run)),
    run.status === "done" ? el("a", {class: "button", href: `#evidence/${run.id}`}, "查看业务证据") : null,
    cancelPanel(run, user, onSettled));
}

export async function runsPage(config, route, signal, user) {
  const items = await api.runs();
  const latest = route.id ? await api.run(route.id, signal) : items[0];
  let current = latest;
  if (!current) {
    return el("div", {class: "page"},
      pageHeading(config.modules[2], config.parameter_label),
      empty("进入资料页，选用一个业务样本并提交执行。"));
  }
  const detail = el("div", {class: "stack"}, ...reportState(current, user, settled => {
    current = settled;
    render();
  }).childNodes);
  function render() {
    detail.replaceChildren(...reportState(current, user, settled => {
      current = settled;
      render();
    }).childNodes);
    document.querySelectorAll(`[data-run="${current.id}"] .status`).forEach(badge =>
      badge.replaceWith(status(current.status)));
  }
  async function refresh() {
    try {
      const next = await api.run(current.id, signal);
      if (!signal.aborted) {
        current = next;
        render();
      }
      return current.status === "queued";
    } catch (error) {
      if (!signal.aborted) detail.append(el("p", {role: "alert"}, error.message));
      return false;
    }
  }
  if (current.status === "queued") poll(refresh, signal);
  return el("div", {class: "page"},
    pageHeading(config.modules[2], config.parameter_label),
    section(current.case_title, detail),
    section("执行批次", items.length ? table(["资料", "编号", "参数", "状态", "提交时间"],
      items.map(run => [el("a", {"data-run": run.id, href: `#runs/${run.id}`}, run.case_title), run.id,
        run.parameter, el("span", {"data-run": run.id}, status(run.status)), time(run.created_at)]), "执行批次") :
      empty("暂无执行记录。")));
}
