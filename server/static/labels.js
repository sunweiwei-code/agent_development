/* 字段/取值/操作的中文词表（学生端与教师端共用）
 *
 * 为什么单独抽一个文件：这些"机器词"会同时出现在教师页的变更预览、审计记录、
 * 学生卡片和对比表里。分散在各页面翻译，必然出现"这页翻了那页没翻"。
 */

window.LB = {
  /* 字段名 */
  field: {
    name: "实验室名称",
    intro: "实验室简介",
    research_areas: "研究方向",
    achievements: "代表成果",
    group_tags: "组别",
    recruit_info: "招新信息",
    contact: "联系方式",
    teacher: "指导老师",
    location: "地点",
    direction_notes: "方向说明",
    path_type: "培养类型",
  },

  /* 对象字段的子键 */
  sub: {
    window: "招新时间",
    accepts_freshman: "是否招大一",
    note: "补充说明",
    qq_group: "QQ 群",
    email: "邮箱",
    wechat: "微信",
  },

  /* 枚举取值 */
  value: {
    yes: "招大一",
    likely: "大概招大一",
    senior_only: "只招大二以上",
    unclear: "未说明",
    "科研主导": "科研主导",
    "竞赛主导": "竞赛主导",
    "就业项目主导": "就业项目主导",
    "双主导": "双主导",
    "教学平台": "教学平台",
    "待确认": "待确认",
  },

  /* 操作语义 */
  op: {
    merge: "修改其中几项",
    replace: "整体替换",
    append: "新增一条",
    remove: "删除一条",
  },
};

/* 失败码 → 人话。直接显示 field_not_allowed 这种串，等于把内部实现抛给老师 */
window.LB.code = {
  field_not_allowed: "这个字段不允许老师修改",
  proposal_invalid: "改动内容不符合该字段的规则",
  lab_mismatch: "只能修改自己实验室的档案",
  stale_value: "档案刚被改过，请重新生成提案",
  dup_append: "这条内容已经在档案里了",
  value_not_found: "档案里找不到要删除的内容",
  not_confirmed: "还没有确认",
  snapshot_not_found: "找不到这个快照",
  clarify: "信息不够，需要补充",
  nochange: "档案里已经是这个数字了",
  confirm: "这个数字需要确认一下",
};

window.cLabel = c => {
  if (!c) return "";
  const key = String(c).split(":")[0].split("（")[0].trim();
  const rest = String(c).includes("：") ? String(c).split("：").slice(1).join("：") : "";
  return (window.LB.code[key] || c) + (rest ? "（" + rest + "）" : "");
};

window.fLabel = f => window.LB.field[f] || f || "";
window.sLabel = k => window.LB.sub[k] || k || "";
window.vLabel = v => (v === null || v === undefined || v === "") ? "未填写"
  : (window.LB.value[v] || v);
window.oLabel = o => window.LB.op[o] || o || "";

/* 把任意值渲染成"人话"：数组列成顿号、对象列成 k:v、字符串过长截断 */
window.human = (v, max) => {
  const cut = s => {
    s = String(s);
    return max && s.length > max ? s.slice(0, max) + "…" : s;
  };
  if (v === null || v === undefined || v === "") return "未填写";
  if (Array.isArray(v)) return v.length ? v.map(x => cut(vLabel(x))).join("、") : "暂无";
  if (typeof v === "object") {
    return Object.entries(v)
      .filter(([, x]) => x !== null && x !== "" && x !== undefined)
      .map(([k, x]) => `${sLabel(k)}：${vLabel(x)}`).join("；") || "未填写";
  }
  return cut(vLabel(v));
};
