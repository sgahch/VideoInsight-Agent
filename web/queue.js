(function () {
  "use strict";

  const STATUS = { queued: "排队中", running: "处理中", succeeded: "已完成", failed: "失败" };
  const STAGES = { downloading: "下载中", transcribing: "转写中", analyzing: "AI 分析中", done: "已完成" };
  const MODES = { summary: "AI 总结", transcript: "逐字稿", interview: "观点提取" };
  const PLATFORMS = [
    { match: /(^|\.)douyin\.com$/, name: "抖音", file: "douyin.ico" },
    { match: /(^|\.)(bilibili\.com|b23\.tv)$/, name: "哔哩哔哩", file: "bilibili.ico" },
    { match: /(^|\.)(kuaishou\.com|gifshow\.com)$/, name: "快手", file: "kuaishou.ico" },
    { match: /(^|\.)(xiaohongshu\.com|xhslink\.com)$/, name: "小红书", file: "xiaohongshu.png" },
  ];
  const $ = (id) => document.getElementById(id);
  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  window.VideoQueue = {
    create(options) {
      let tasks = [];
      let filter = "all";
      let search = "";
      let page = 1;
      let selectedId = null;
      let timer = null;
      let loading = null;
      let pendingRefresh = false;
      let refreshFailed = false;
      let stopped = false;
      const pageSize = 8;

      function notify(message, error = false) {
        $("queueNotice").hidden = !message;
        $("queueNotice").classList.toggle("is-error", error);
        $("queueNotice").textContent = message;
      }

      function showHome(push = true) {
        options.stopWatching();
        selectedId = null;
        document.body.dataset.view = "queue";
        delete document.body.dataset.detailLoading;
        $("taskDetailHeader").hidden = true;
        if (push && location.pathname !== "/") history.pushState({}, "", "/");
        document.title = "任务工作台 · VideoInsight Agent";
        $("queueHomeLink").classList.add("active");
        renderTasks();
        refresh();
      }

      function showDetail(taskId) {
        selectedId = taskId;
        document.body.dataset.view = "detail";
        document.body.dataset.detailLoading = "true";
        $("taskDetailHeader").hidden = false;
        $("taskDetailTitle").textContent = "正在加载任务…";
        $("taskDetailMeta").textContent = taskId;
        $("taskDetailNotice").hidden = true;
        const url = `/view/${encodeURIComponent(taskId)}`;
        if (location.pathname !== url) history.pushState({}, "", url);
        $("taskDetailHeader").scrollIntoView({ block: "start" });
      }

      function onTask(task) {
        const index = tasks.findIndex((item) => item.task_id === task.task_id);
        const summary = {
          task_id: task.task_id, status: task.status, request: task.request,
          created_at: task.created_at, updated_at: task.updated_at, used_at: options.usedAt(task),
          title: options.title(task), progress: task.progress, stage: task.stage, error: task.error,
        };
        if (index >= 0) tasks[index] = { ...tasks[index], ...summary };
        else {
          tasks.push(summary);
          tasks.sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
        }
        options.onTasks(tasks.slice(0, 50));
        if (selectedId !== task.task_id) return;
        delete document.body.dataset.detailLoading;
        $("taskDetailTitle").textContent = options.title(task);
        $("taskDetailMeta").textContent = `${MODES[task.request?.mode] || "视频分析"} · ${STATUS[task.status] || task.status} · ${task.task_id.slice(0, 8)}`;
        $("taskDetailNotice").hidden = true;
        document.title = `${options.title(task)} · VideoInsight`;
      }

      function onTaskUsed(taskId, usedAt) {
        const task = tasks.find((item) => item.task_id === taskId);
        if (task) task.used_at = usedAt;
        renderTasks();
        options.onTasks(tasks.slice(0, 50));
      }

      function onTaskError(message) {
        $("taskDetailTitle").textContent = "无法打开任务";
        $("taskDetailNotice").hidden = false;
        $("taskDetailNotice").textContent = message;
        $("taskDetailNotice").classList.add("is-error");
      }

      function source(task) {
        const url = task.request?.url || "";
        if (url.startsWith("upload://")) return { name: "本地文件", label: url.slice(9) };
        try {
          const host = new URL(url).hostname;
          const platform = PLATFORMS.find((item) => item.match.test(host));
          return { ...(platform || { name: host }), label: task.request?.label || url };
        } catch (error) {
          return { name: "链接", label: url };
        }
      }

      function iconButton(symbol, label, action) {
        const button = element("button", "queue-icon-button", symbol);
        button.type = "button";
        button.title = label;
        button.setAttribute("aria-label", label);
        button.addEventListener("click", action);
        return button;
      }

      function taskRow(task) {
        const row = element("article", "queue-task-row");
        row.dataset.status = task.status;
        row.dataset.taskId = task.task_id;
        const link = element("a", "queue-task-link");
        link.href = `/view/${encodeURIComponent(task.task_id)}`;
        link.dataset.taskId = task.task_id;
        link.setAttribute("aria-label", `${options.title(task)}，${STATUS[task.status] || task.status}`);
        const platform = source(task);
        if (platform.file) {
          const image = element("img", "queue-task-logo");
          image.src = `/static/assets/platforms/${platform.file}`;
          image.width = 30;
          image.height = 30;
          image.alt = platform.name;
          link.append(image);
        } else {
          const mark = element("span", "queue-task-logo queue-task-fallback", platform.name === "本地文件" ? "AV" : "↗");
          mark.setAttribute("aria-hidden", "true");
          link.append(mark);
        }
        const content = element("div");
        const title = element("span", "queue-task-title", options.title(task));
        title.title = options.title(task);
        const subtitle = element("span", "queue-task-source", `${platform.name} · ${platform.label}`);
        subtitle.title = platform.label;
        content.append(title, subtitle);
        const stage = element("div", "queue-task-stage");
        let label = STATUS[task.status] || task.status;
        if (task.status === "running") {
          label = STAGES[task.stage] || "准备处理";
          if (Number.isFinite(task.progress)) label += ` · ${Math.round(task.progress)}%`;
        } else if (task.status === "queued" && task.queue_position) {
          label = `排队第 ${task.queue_position} 位`;
        } else if (task.status === "failed") {
          label = task.error || "处理失败";
        }
        const stageLabel = element("span", "queue-task-stage-label", label);
        stageLabel.title = label;
        stage.append(stageLabel);
        const usedAt = options.usedAt(task);
        if (usedAt) {
          const badge = element("span", "task-used-badge", "已使用");
          badge.title = `首次复制：${new Date(usedAt).toLocaleString("zh-CN")}`;
          stage.append(badge);
        }
        if (task.status === "running") {
          const progress = element("progress", "queue-task-progress");
          progress.max = 100;
          if (Number.isFinite(task.progress)) progress.value = task.progress;
          progress.setAttribute("aria-label", `${options.title(task)}处理进度`);
          stage.append(progress);
        }
        const date = new Date(task.created_at);
        const time = element("time", "queue-task-time", Number.isNaN(date.valueOf()) ? "" : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }));
        link.append(content, stage, time);
        link.addEventListener("click", (event) => {
          if (event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
          event.preventDefault();
          options.openTask(task.task_id);
        });
        const actions = element("div", "queue-task-actions");
        if (task.status === "failed" && !task.request?.url?.startsWith("upload://")) {
          actions.append(iconButton("↻", "重新提交", () => options.retry(task)));
        }
        if (task.status !== "running") {
          actions.append(iconButton("×", task.status === "queued" ? "移出等待队列" : "删除任务", async () => {
            if (!window.confirm(task.status === "queued" ? "将此任务移出等待队列？" : "删除此任务及其分析结果？")) return;
            try {
              await options.fetchJson(`/tasks/${task.task_id}`, { method: "DELETE" });
              notify("任务已删除。");
              await refresh(true);
            } catch (error) { notify(error.message, true); }
          }));
        } else {
          const arrow = element("a", "queue-icon-button", "→");
          arrow.href = link.href;
          arrow.title = "查看视频详情";
          arrow.setAttribute("aria-label", "查看视频详情");
          arrow.addEventListener("click", (event) => {
            if (event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
            event.preventDefault();
            options.openTask(task.task_id);
          });
          actions.append(arrow);
        }
        row.append(link, actions);
        return row;
      }

      function renderTasks() {
        const focused = document.activeElement?.closest(".queue-task-row")?.dataset.taskId;
        const visible = tasks.filter((task) =>
          (filter === "all" || task.status === filter) &&
          `${options.title(task)} ${task.request?.url || ""} ${task.request?.label || ""}`.toLocaleLowerCase().includes(search)
        ).sort((a, b) => {
          const priority = { running: 0, queued: 1, failed: 2, succeeded: 3 };
          if (filter === "all" && priority[a.status] !== priority[b.status]) return priority[a.status] - priority[b.status];
          if (a.status === "queued" && b.status === "queued") return (a.queue_position || Infinity) - (b.queue_position || Infinity);
          return 0;
        });
        const pages = Math.max(1, Math.ceil(visible.length / pageSize));
        page = Math.max(1, Math.min(page, pages));
        const start = (page - 1) * pageSize;
        const nodes = visible.slice(start, start + pageSize).map(taskRow);
        if (!nodes.length) nodes.push(element("div", "queue-empty", tasks.length ? "没有匹配的任务" : "暂无任务"));
        $("queueTaskList").replaceChildren(...nodes);
        if (focused) $("queueTaskList").querySelector(`a[data-task-id="${CSS.escape(focused)}"]`)?.focus({ preventScroll: true });
        $("queuePageLabel").textContent = `${visible.length ? start + 1 : 0}–${Math.min(start + pageSize, visible.length)} / ${visible.length} 条 · 最近 200 条`;
        $("queuePageNumber").textContent = `${page} / ${pages}`;
        $("queuePrevious").disabled = page === 1;
        $("queueNext").disabled = page === pages;
      }

      function renderOverview(data) {
        const counts = data.counts;
        $("queueRunningCount").textContent = counts.running;
        $("queueWaitingCount").textContent = counts.queued;
        $("queueDoneCount").textContent = counts.succeeded;
        $("queueFailedCount").textContent = counts.failed;
        const pool = data.pool;
        $("poolCore").textContent = pool.core_workers;
        $("poolMax").textContent = pool.max_workers;
        $("poolActive").textContent = pool.active_workers;
        $("poolWaiting").textContent = `${pool.queued} / ${pool.queue_capacity}`;
        $("poolAsr").textContent = data.engine.asr_global_workers;
        $("queueEngine").textContent = "转写 · 云端 / 总结 · API";
        const slots = [];
        for (let index = 0; index < Math.min(pool.max_workers, 24); index++) {
          const busy = index < pool.active_workers;
          const overflow = index >= pool.core_workers;
          const slot = element("span", `queue-slot${busy ? " is-active" : ""}${overflow ? " is-overflow" : ""}`, busy ? "执行中" : overflow ? "扩展" : "空闲");
          slot.title = `${overflow ? "扩展" : "核心"}执行槽位 ${index + 1} · ${busy ? "已占用" : "未占用"}`;
          slots.push(slot);
        }
        $("poolSlots").replaceChildren(...slots);
        $("poolBuffer").max = pool.queue_capacity;
        $("poolBuffer").value = pool.queued;
        $("poolBufferLabel").textContent = `${pool.queued}/${pool.queue_capacity}`;
        tasks = data.tasks;
        options.onTasks(tasks.slice(0, 50));
        renderTasks();
        $("queueConnection").textContent = "实时更新";
        $("queueConnection").classList.remove("is-stale");
      }

      function refresh(force = false) {
        if (loading) {
          if (force) pendingRefresh = true;
          return loading;
        }
        window.clearTimeout(timer);
        $("queueRefresh").disabled = true;
        loading = (async () => {
          try {
            renderOverview(await options.fetchJson("/queue?limit=200", {}, 10000));
            if (refreshFailed) notify("");
            refreshFailed = false;
          } catch (error) {
            refreshFailed = true;
            $("queueConnection").textContent = "连接已断开";
            $("queueConnection").classList.add("is-stale");
            notify(`队列更新失败：${error.message}`, true);
            options.onRefreshError(error);
            if (!tasks.length) $("queueTaskList").replaceChildren(element("div", "queue-empty", "暂时无法加载任务"));
          } finally {
            loading = null;
            $("queueRefresh").disabled = false;
            if (!stopped) {
              const background = document.hidden;
              timer = window.setTimeout(refresh, pendingRefresh ? 0 : background ? 15000 : 2000);
              pendingRefresh = false;
            }
          }
        })();
        return loading;
      }

      function route() {
        const match = location.pathname.match(/^\/view\/([a-zA-Z0-9_-]+)\/?$/);
        if (match) options.openTask(decodeURIComponent(match[1]));
        else showHome(false);
      }

      $("queueRefresh").addEventListener("click", () => refresh(true));
      $("queueHomeLink").addEventListener("click", (event) => { event.preventDefault(); showHome(); window.scrollTo({ top: 0 }); });
      $("backToQueue").addEventListener("click", (event) => { event.preventDefault(); showHome(); window.scrollTo({ top: 0 }); });
      $("queueFilters").addEventListener("click", (event) => {
        const button = event.target.closest("[data-queue-filter]");
        if (!button) return;
        filter = button.dataset.queueFilter;
        page = 1;
        $("queueFilters").querySelectorAll("button").forEach((item) => item.setAttribute("aria-selected", String(item === button)));
        renderTasks();
      });
      $("queueFilters").addEventListener("keydown", (event) => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        const buttons = [...$("queueFilters").querySelectorAll("button")];
        const index = buttons.indexOf(document.activeElement);
        const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + buttons.length) % buttons.length;
        event.preventDefault();
        buttons[next].click();
        buttons[next].focus();
      });
      $("queueSearch").addEventListener("input", (event) => { search = event.target.value.trim().toLocaleLowerCase(); page = 1; renderTasks(); });
      $("queuePrevious").addEventListener("click", () => { page -= 1; renderTasks(); });
      $("queueNext").addEventListener("click", () => { page += 1; renderTasks(); });
      window.addEventListener("popstate", route);
      document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(true); });
      window.addEventListener("pagehide", (event) => {
        if (!event.persisted) { stopped = true; window.clearTimeout(timer); }
      });

      return {
        showHome, showDetail, onTask, onTaskUsed, onTaskError, notify, refresh,
        prepare() {
          const match = location.pathname.match(/^\/view\/([a-zA-Z0-9_-]+)\/?$/);
          if (match) showDetail(match[1]);
          else document.body.dataset.view = "queue";
        },
        start() { route(); refresh(); },
        async onSubmitted(taskId) {
          if (selectedId) showHome();
          notify(`任务 ${taskId.slice(0, 8)} 已加入处理队列。`);
          await refresh(true);
          $("queueDashboard").scrollIntoView({ block: "start" });
        },
      };
    },
  };
})();
