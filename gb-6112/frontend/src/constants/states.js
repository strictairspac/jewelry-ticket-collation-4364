export const States = Object.freeze({
  queued: "等待执行", done: "已完成", failed: "执行失败", cancelled: "已撤单",
});

export const Actions = Object.freeze({
  create_case: "登记资料", enqueue: "提交执行", done: "执行完成", failed: "执行失败",
  cancel: "撤销批次", review: "复核签署",
});
