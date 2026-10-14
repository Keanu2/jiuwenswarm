/**
 * Designer leader 流式渲染桥（已弃用）。
 *
 * team.design 的 leader 回合经 DesignTeamBridge 走标准 team 流式通道，
 * chat.delta / chat.final / chat.tool_call / chat.tool_result 等事件由
 * 顶层 useWebSocket 统一处理并写入 useChatStore，designer 聊天面板直接
 * 读取 useChatStore 渲染。本模块保留空函数以兼容旧调用点。
 */

/** 开始一轮 leader 流式：仅作状态标记。 */
export function beginDesignerLeaderStream(_sessionId: string): void {
  // noop: ws 事件由 useWebSocket 统一处理。
}

/** 结束一轮：仅作状态标记。 */
export function endDesignerLeaderStream(): void {
  // noop: ws 事件由 useWebSocket 统一处理。
}

/** 绑定 ws 事件：已废弃，返回空 unbind。 */
export function bindDesignerLeaderStream(): () => void {
  // noop: ws 事件由 useWebSocket 统一处理。
  return () => undefined;
}
