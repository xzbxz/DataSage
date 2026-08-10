# -*- coding: utf-8 -*-
"""通用关键字分类器骨架：问题语料 × 能力目录审计。
用法: python classify_questions.py <corpus.json> [out_dir]
语料结构: {user_id: [{"time": str, "question": str}, ...]}

要点（全部实测踩坑）:
- 分类优先级顺序是关键: NOISE -> CAP -> XL -> EXP -> UNS -> TGT -> AR -> INV -> RCP -> CMP
  -> DLV -> ORD -> MD -> MT -> 兜底 DLV。UNS 必须在领域类之前, 否则"利润/采购额"被
  `排名|top|销售` 抢走。
- 覆盖表 OV 必须用【完整 user_id】做 key —— WeCom 用户 ID 前 8 位完全相同(wo8gxKDA...),
  截断会静默改错别的用户的行。
- UNS_KW 为本语料调优, 每次审计需按能力目录复核增删。
"""
import json, re, sys, collections

CATS = {
 'DLV':'出库/销售', 'RCP':'收款', 'AR':'应收/欠款/风险', 'TGT':'目标完成',
 'INV':'库存/备货/滞销', 'MD':'主数据', 'ORD':'订单/单据明细', 'CMP':'对比分析',
 'MT':'多轮追问/指代', 'CAP':'功能询问/寒暄/质疑', 'XL':'跨语言', 'EXP':'导出/文档',
 'NOISE':'噪声/系统消息', 'UNS':'当前不支持/超能力'}

VN = re.compile(r'[àáạảãăắằặẳẵâầấậẩẫđèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹ]')
CJK = re.compile(r'[\u4e00-\u9fff]')
EN_Q = re.compile(r'\b(based on|payment collection|who is the top|top five|how many|what is|compare)\b', re.I)

# 六域外指标族（DataSage 2026-07 实测），每次审计需复核
UNS_KW = ['利润','赚了多少钱','人效','物流线','样品','样卡','SQ','采购额','采购数据','采购情况','客诉',
          '拜访','对账单','定金','灵犀','原数据','资金周转率','库存周转率','出库计划','收款计划','发货计划',
          '单价','什么价格','价格怎么样','账期要控制','主推产品','票数','收货人','公海','铜氨面料','新客户',
          '新增客户数','新增下单客户数','SQ下单','样品入库']

def classify(q):
    ql = q.strip().lower()
    flags = set()
    has_cjk = bool(CJK.search(q))
    has_emoji = any(0x1F000 <= ord(c) <= 0x1FAFF for c in q)
    # 噪声
    if len(ql) <= 6 and (('合十' in q) or ('胜利' in q) or ('呲牙' in q) or has_emoji): return 'NOISE', flags
    if ql.startswith("' reply_to_id") or ('reply_to_id' in ql and len(ql) < 60): return 'NOISE', flags
    if ql.startswith('[important:') or 'background process' in ql: return 'NOISE', flags
    if ql.startswith('好的，关于「') or ql.startswith('order & delivery terms'): return 'NOISE', flags
    # 跨语言
    if VN.search(q):
        flags.add('xl_vn')
        if re.search(r'用 VND|以 VND|越南语表示|中文表达|印尼语', q) and len(q) < 30: return 'MT', flags
        return 'XL', flags
    if (not has_cjk) and EN_Q.search(q):
        flags.add('xl_en'); return 'XL', flags
    # 功能/寒暄/质疑
    if re.search(r'你是谁|nishishui|你有哪些功能|你是干什么|什么机器人|有什么功能|我可以问什么|面向什么角色|解决什么问题|你还在吗|^hi$|^helo$|^hello$|^datasage$', ql): return 'CAP', flags
    if re.search(r'平均回款天数是怎么计算|是怎么计算的|你为啥会用错公式|怎么算的|上一轮查得.*不一样|数据肯定不对|你重新检查|BI里不是已经有了吗|为什么不能导出|结果没有看到|怎么没有显示|那我如何查询', q): return 'CAP', flags
    # 纯导出指令
    if re.search(r'excel|导出|生成.*(表|文件)|整理成.*表|word文档|word表格|形成文档|供我下载|汇总成一个', q, re.I):
        flags.add('exp')
        if not re.search(r'查|统计|多少|排名|情况|收款|出库|库存|采购|销售|目标|欠款|应收|备货|滞销|下单|发货|出货|客户|产品|供应商|明细', q) or len(q) < 14: return 'EXP', flags
    # 六域外指标（必须在领域类之前）
    if any(k in q for k in UNS_KW): return 'UNS', flags
    # 领域类
    if re.search(r'目标|指标', q): return 'TGT', flags
    if re.search(r'应收|欠款|账龄|周转|风险|信用额度|回款天数|欠账|账期', q): return 'AR', flags
    if re.search(r'库存|库龄|备货|补货|滞销|在途|存货|slow', q, re.I): return 'INV', flags
    if re.search(r'收款|回款|收了多少|收了多少钱|收入|collection|收到', q, re.I): return 'RCP', flags
    if re.search(r'对比|比较|同比|环比|增幅|vs|versus|增长|下降|损失|流失|波动|同期|变化', q, re.I): return 'CMP', flags
    if re.search(r'订单号|这个订单|下单金额|订单数据|订单明细|订单情况|B[0-9]{8}', q): return 'ORD', flags
    if re.search(r'供应商|客户有多少|多少家|客户分布|客户名|公司编号|编号是多少|多少个供应商|浮光锦|价格', q): return 'MD', flags
    if re.search(r'出库|发货|出货|销售|销量|下单|订单|客户数|产品数|销售额|排名|top|营业额|业绩|明细', q, re.I): return 'DLV', flags
    # 多轮/短指代
    if len(q.strip()) <= 10 or re.search(r'呢$|可以$|继续$|需要$|^[1-4]$|明细看下|两者都查|客户名称|按各市场分别统计|只看越南区域的|统计大货|月度趋势|用 VND|以 VND|用人民币|原币|印尼盾|泰铢|VND 表示|中文表达|印尼语|越南语表示|这 ?3 个|这四个部门|的对比情况|去年同期|主要针对国外', q): return 'MT', flags
    return 'DLV', flags

# ============ 人工覆盖表: (完整user_id, idx) -> (cat, note) ============
# 例: OV = {('wo8gxKDAAA0e36LXk9vXJ_nmbnmE2gzg', 22): ('MT', 'SKU明细=滞销上下文追问')}
OV = {}

def main(path, out_dir='.'):
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    results = []
    for u, qs in data.items():
        for i, q in enumerate(qs):
            key = (u, i)
            if key in OV:
                cat, note = OV[key]
            else:
                cat, _ = classify(q['question']); note = ''
            results.append((u, i, cat, note, q['time'], q['question']))
    cnt = collections.Counter(r[2] for r in results)
    total = len(results)
    print('=== 分类统计 ===')
    for k in CATS:
        print(f'{CATS[k]}: {cnt.get(k,0)}')
    print('总计:', total)
    assert sum(cnt.values()) == total, '类别合计 != 语料总数, 有漏分!'
    detail = rf'{out_dir}/wecom_classified_final.txt'
    with open(detail, 'w', encoding='utf-8') as f:
        for u, i, cat, note, t, q in results:
            f.write(f'{u}|#{i}|{cat}|{t}|{q}|{note}\n')
    print('明细已写', detail)
    # 专项清单
    for name, pred in [('uns', lambda c: c=='UNS'), ('mt', lambda c: c=='MT'),
                       ('xl', lambda c: c=='XL'), ('exp', lambda c: c=='EXP'),
                       ('noise', lambda c: c=='NOISE')]:
        lines = [f'{u}|{t}|{q}' for u, i, c, n, t, q in results if pred(c)]
        p = rf'{out_dir}/list_{name}.txt'
        with open(p, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
        print(f'list_{name}.txt: {len(lines)} 条')

if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else '.')
