"""Actual source-backed fabric workbook and charts; no responsibility inference."""
from pathlib import Path
from decimal import Decimal
from xml.etree import ElementTree as ET
import zipfile,math,json
from .legacy_xlsx import gen_workbook_xlsx
from . import result_completeness

FIELDS={'metric_value':'源记录数','fabric_rolls':'完整卷数','fabric_known_rolls':'已知卷数','fabric_tagged_rolls':'完整标签卷数','fabric_known_tagged_rolls':'已知标签卷数','fabric_ddp_rmb':'完整DDP人民币','fabric_known_ddp_rmb':'已知DDP人民币','fabric_tagged_ddp_rmb':'完整标签DDP人民币','fabric_known_tagged_ddp_rmb':'已知标签DDP人民币','fabric_sales_rmb':'完整销售人民币','fabric_known_sales_rmb':'已知销售人民币','fabric_tagged_ddp_gap_rmb':'完整标签DDP差额','fabric_known_tagged_ddp_gap_rmb':'已知标签DDP差额','fabric_tag_rate':'自身标签发生率','fabric_tag_contribution':'标签卷数贡献率','fabric_missing_rolls':'缺卷数行','fabric_unknown_tags':'未知标签行','fabric_formation_pending':'形成时期待核验行','fabric_multiple_roots':'多根血缘行','fabric_scope_unknown':'范围待核验行','fabric_read_at':'读取时点','fabric_etl_min':'最早ETL','fabric_etl_max':'最晚ETL'}

def n(value):
    if value is None:return None
    try:
        x=Decimal(str(value));return x if x.is_finite() else None
    except Exception:return None

def _identity_fields(dimensions):
    refs=[];states=[]
    for dimension in dimensions if isinstance(dimensions,list) else []:
        if not isinstance(dimension,dict):continue
        for key in ('entity_ref','identity_ref','stable_entity_ref'):
            value=dimension.get(key)
            if value not in (None,''):refs.append(str(value))
        value=dimension.get('identity_state')
        if value not in (None,''):states.append(str(value))
    return refs,states


def _row(row):
    if not isinstance(row,dict):return {'group':'总体','facts':{}}
    dimensions=row.get('dimensions') if isinstance(row.get('dimensions'),list) else []
    values=[str(d.get('value')) for d in dimensions if isinstance(d,dict) and d.get('value') not in (None,'')]
    refs,states=_identity_fields(dimensions)
    result={'group':' / '.join(values) or str(row.get('group') or '总体'),'facts':row.get('facts',{}) if isinstance(row.get('facts'),dict) else {}}
    if refs:result['identity_refs']='；'.join(dict.fromkeys(refs))
    if states:result['identity_states']='；'.join(dict.fromkeys(states))
    for key in ('currency','currency_no'):
        value=row.get(key)
        if value not in (None,''):result[key]=str(value)
    return result


def _item(name,result,*,packet_status=None,preview=False):
    result=result if isinstance(result,dict) else {}
    raw={**result}
    if packet_status is not None:
        if packet_status!='success':raw['status']=packet_status
        elif 'status' not in raw:raw['status']='missing_result_status'
    rows=raw.get('rows') if isinstance(raw.get('rows'),list) else []
    raw['rows']=rows
    raw.setdefault('row_count',len(rows))
    coverage=result_completeness.summarize_result(raw)
    if preview:
        coverage={**coverage,'completeness':'limited','report_complete':False,
                  'unknown_items':{**coverage.get('unknown_items',{}),'preview_only':'RAW_QUERY_EVIDENCE_MISSING'}}
    return {'name':name,'rows':[_row(row) for row in rows],
        'truncated':raw.get('truncated'),'source':'legacy_observation_preview' if preview else 'existing_governed_query',
        'time':raw.get('applied_time_range'),'coverage':coverage,'_result':raw}


def observations(doc):
    if isinstance(doc.get('observations'),list):
        return [_item(item.get('name','观察表'),item,preview=True) for item in doc['observations'] if isinstance(item,dict)]
    result=[];names=['出库总览','出库渠道','出库形成时期','库存总览','库存渠道','库存形成时期']
    expected=doc.get('expected_request_ids')
    if isinstance(doc.get('query_packets'),list) and isinstance(expected,list) and all(isinstance(item,str) for item in expected):
        raw_by_id={};packet_status_by_id={}
        for packet in doc['query_packets']:
            if not isinstance(packet,dict) or not isinstance(packet.get('results'),list):continue
            for item in packet['results']:
                if isinstance(item,dict) and isinstance(item.get('request_id'),str) and item['request_id'] not in raw_by_id:
                    raw_by_id[item['request_id']]=item
                    packet_status_by_id[item['request_id']]=packet.get('status')
        for i,request_id in enumerate(expected):
            if request_id in raw_by_id:
                packet_status=packet_status_by_id.get(request_id) or 'missing_packet_status'
                result.append(_item(names[i] if i<len(names) else str(i),raw_by_id[request_id],packet_status=packet_status))
            else:
                result.append(_item(names[i] if i<len(names) else str(i),{'request_id':request_id,'status':'missing_result','data_state':'incomplete'},packet_status='missing_result'))
        return result
    for i,packet in enumerate(doc.get('query_packets',[]) if isinstance(doc.get('query_packets'),list) else []):
        if not isinstance(packet,dict):
            result.append(_item(names[i] if i<len(names) else str(i),{'status':'missing_result','data_state':'incomplete'},packet_status='missing_result'));continue
        rows=packet.get('results')
        if not isinstance(rows,list) or not rows:
            result.append(_item(names[i] if i<len(names) else str(i),{'status':packet.get('status','missing_result'),'data_state':'incomplete'},packet_status=packet.get('status','missing_result')))
            continue
        for j,item in enumerate(rows):
            name=names[i] if i<len(names) and j==0 else (names[i]+'#'+str(j+1) if i<len(names) else str(i)+'#'+str(j+1))
            result.append(_item(name,item,packet_status=packet.get('status') or 'missing_packet_status'))
    return result

def chart(path,title,rows,series):
    from PIL import Image,ImageDraw,ImageFont
    font_path=Path('C:/Windows/Fonts/msyh.ttc');font=ImageFont.truetype(str(font_path),15);bold=ImageFont.truetype(str(font_path),19)
    height=115+len(rows)*65;im=Image.new('RGB',(1000,max(180,height)),'white');d=ImageDraw.Draw(im)
    d.text((20,15),title,font=bold,fill='#172d46')
    colors=['#24679c','#bd7433'];values=[float(n(r['facts'].get(f)) or 0) for r in rows for f,_ in series]
    rate=all(f in ('fabric_tag_rate','fabric_tag_contribution') for f,_ in series);maximum=max(values+[1])
    for k,(_,label) in enumerate(series):d.rectangle((20+k*270,51,35+k*270,66),fill=colors[k]);d.text((43+k*270,47),label,font=font,fill='#172d46')
    for i,row in enumerate(rows):
        y=91+i*65;d.text((20,y),str(row['group']),font=font,fill='#172d46')
        for j,(field,_) in enumerate(series):
            value=n(row['facts'].get(field));yy=y+j*25
            if value is None:d.text((230,yy),'未知，不填零',font=font,fill='#855c38');continue
            if value<0:d.text((230,yy),'负值需核对：'+str(value),font=font,fill='#855c38');continue
            width=int(float(value)/maximum*610)
            if width>0:d.rectangle((230,yy,230+width,yy+17),fill=colors[j])
            d.text((850,yy-2),f'{float(value)*100:.1f}%' if rate else str(value),font=font,fill='#172d46')
    im.save(path,'PNG')

def embed_charts(book,images,start_row):
    # Small fixed drawing attachment, reusing the existing workbook emitter.
    ns='http://schemas.openxmlformats.org/spreadsheetml/2006/main';rel='http://schemas.openxmlformats.org/officeDocument/2006/relationships';pkg='http://schemas.openxmlformats.org/package/2006/relationships';draw='http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing';a='http://schemas.openxmlformats.org/drawingml/2006/main'
    ET.register_namespace('',ns);ET.register_namespace('r',rel);ET.register_namespace('xdr',draw);ET.register_namespace('a',a)
    with zipfile.ZipFile(book) as z:parts={name:z.read(name) for name in z.namelist()}
    sheet=ET.fromstring(parts['xl/worksheets/sheet1.xml']);ET.SubElement(sheet,'{'+ns+'}drawing',{'{'+rel+'}id':'rIdCharts'});parts['xl/worksheets/sheet1.xml']=ET.tostring(sheet,encoding='utf-8',xml_declaration=True)
    relations=ET.Element('Relationships',xmlns=pkg);ET.SubElement(relations,'Relationship',Id='rIdCharts',Type=rel+'/drawing',Target='../drawings/drawing1.xml');parts['xl/worksheets/_rels/sheet1.xml.rels']=ET.tostring(relations,encoding='utf-8',xml_declaration=True)
    drawing=ET.Element('{'+draw+'}wsDr');rels=ET.Element('Relationships',xmlns=pkg)
    from PIL import Image
    for i,path in enumerate(images,1):
        with Image.open(path) as im:w,h=im.size
        anchor=ET.SubElement(drawing,'{'+draw+'}twoCellAnchor',editAs='oneCell');frm=ET.SubElement(anchor,'{'+draw+'}from')
        for tag,value in [('col',0),('colOff',0),('row',start_row),('rowOff',0)]:ET.SubElement(frm,'{'+draw+'}'+tag).text=str(value)
        to=ET.SubElement(anchor,'{'+draw+'}to')
        for tag,value in [('col',10),('colOff',0),('row',start_row+math.ceil(h/20)),('rowOff',0)]:ET.SubElement(to,'{'+draw+'}'+tag).text=str(value)
        pic=ET.SubElement(anchor,'{'+draw+'}pic');nv=ET.SubElement(pic,'{'+draw+'}nvPicPr');ET.SubElement(nv,'{'+draw+'}cNvPr',id=str(i),name='Source chart '+str(i));ET.SubElement(nv,'{'+draw+'}cNvPicPr')
        fill=ET.SubElement(pic,'{'+draw+'}blipFill');ET.SubElement(fill,'{'+a+'}blip',{'{'+rel+'}embed':'rId'+str(i)});stretch=ET.SubElement(fill,'{'+a+'}stretch');ET.SubElement(stretch,'{'+a+'}fillRect');sp=ET.SubElement(pic,'{'+draw+'}spPr');xf=ET.SubElement(sp,'{'+a+'}xfrm');ET.SubElement(xf,'{'+a+'}off',x='0',y='0');ET.SubElement(xf,'{'+a+'}ext',cx=str(w*9525),cy=str(h*9525));geo=ET.SubElement(sp,'{'+a+'}prstGeom',prst='rect');ET.SubElement(geo,'{'+a+'}avLst');ET.SubElement(anchor,'{'+draw+'}clientData')
        ET.SubElement(rels,'Relationship',Id='rId'+str(i),Type=rel+'/image',Target='../media/chart'+str(i)+'.png');parts['xl/media/chart'+str(i)+'.png']=Path(path).read_bytes();start_row+=math.ceil(h/20)+2
    parts['xl/drawings/drawing1.xml']=ET.tostring(drawing,encoding='utf-8',xml_declaration=True);parts['xl/drawings/_rels/drawing1.xml.rels']=ET.tostring(rels,encoding='utf-8',xml_declaration=True)
    types=ET.fromstring(parts['[Content_Types].xml']);t='http://schemas.openxmlformats.org/package/2006/content-types';ET.register_namespace('',t);ET.SubElement(types,'{'+t+'}Default',Extension='png',ContentType='image/png');ET.SubElement(types,'{'+t+'}Override',PartName='/xl/drawings/drawing1.xml',ContentType='application/vnd.openxmlformats-officedocument.drawing+xml');parts['[Content_Types].xml']=ET.tostring(types,encoding='utf-8',xml_declaration=True)
    temp=book.with_suffix('.tmp')
    with zipfile.ZipFile(temp,'w',zipfile.ZIP_DEFLATED) as z:
        for name,value in parts.items():z.writestr(name,value)
    temp.replace(book)


def embed_coverage_markers(book,markers,start_sheet=2):
    """Keep a plain-text coverage marker on each table worksheet."""
    ns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    with zipfile.ZipFile(book) as z:parts={name:z.read(name) for name in z.namelist()}
    for offset,marker in enumerate(markers):
        name=f'xl/worksheets/sheet{start_sheet+offset}.xml'
        if name not in parts:continue
        sheet=ET.fromstring(parts[name]);data=sheet.find('{'+ns+'}sheetData')
        if data is None:continue
        rows=data.findall('{'+ns+'}row');last=max((int(row.get('r','0')) for row in rows),default=0)+1
        row=ET.SubElement(data,'{'+ns+'}row',r=str(last));cell=ET.SubElement(row,'{'+ns+'}c',r='A'+str(last),t='str');ET.SubElement(cell,'{'+ns+'}v').text=str(marker)
        parts[name]=ET.tostring(sheet,encoding='utf-8',xml_declaration=True)
    temp=Path(book).with_suffix('.tmp')
    with zipfile.ZipFile(temp,'w',zipfile.ZIP_DEFLATED) as z:
        for name,value in parts.items():z.writestr(name,value)
    temp.replace(book)

def export_report(doc,out):
    out=Path(out);out.mkdir(parents=True,exist_ok=True);data=observations(doc);sheets=[];overview=[];overview_formats={};images=[]
    for item in data:
        for row in item['rows']:
            f=row['facts']
            if item['name'].endswith(('总览','overall')):
                for field,label in FIELDS.items():
                    if field in f:
                        overview.append([item['name'],label,n(f[field]) if n(f[field]) is not None else str(f[field]) if f[field] is not None else '未知'])
                        if field in ('fabric_tag_rate','fabric_tag_contribution'):overview_formats[(len(overview),2)]='percent'
        fields=[f for f in FIELDS if any(f in r['facts'] for r in item['rows'])]
        identity_headers=[]
        if any(r.get('identity_refs') for r in item['rows']):identity_headers.append(('identity_refs','维度身份引用'))
        if any(r.get('identity_states') for r in item['rows']):identity_headers.append(('identity_states','维度身份状态'))
        for key,label in (('currency','币种'),('currency_no','源币种')):
            if any(r.get(key) for r in item['rows']):identity_headers.append((key,label))
        coverage_headers=['status','data_state','completeness','truncated','returned_group_count','population_group_count','observed_at','unknown_items']
        headers=['分组']+[FIELDS[f] for f in fields]+[label for _,label in identity_headers]+coverage_headers
        c=item['coverage'];coverage_values=[c['status'],c['data_state'],c['completeness'],c['truncated'],c['returned_group_count'],c['population_group_count'] if c['population_group_count'] is not None else '未知',c['observed_at'] or '未知',json.dumps(c.get('unknown_items') or {},ensure_ascii=False,sort_keys=True,default=str)]
        rows=[[r['group']]+[n(r['facts'].get(f)) if n(r['facts'].get(f)) is not None else str(r['facts'][f]) if r['facts'].get(f) is not None else '未知' for f in fields]+[r.get(key,'未知') for key,_ in identity_headers]+coverage_values for r in item['rows']]
        if not rows:rows=[['覆盖证据']+['']*(len(headers)-1-len(coverage_values))+coverage_values]
        formats={i+1:'percent' for i,f in enumerate(fields) if f in ('fabric_tag_rate','fabric_tag_contribution')}
        sheets.append((item['name'],headers,rows,formats))
        if '渠道' in item['name'] or 'channels' in item['name']:
            for label,series in [('发生率与贡献率',[('fabric_tag_rate','渠道自身发生率'),('fabric_tag_contribution','标签问题贡献率')]),('标签卷数',[('fabric_known_tagged_rolls','源标签卷数已知部分')])]:
                observed=item['rows'][0]['facts'].get('fabric_read_at') if item['rows'] else None
                marker='；'+str(item['coverage']['completeness'])+('；仅返回部分' if item['coverage']['truncated'] is True else '')
                path=out/f'chart-{len(images)+1}.png';chart(path,item['name']+' '+(str(observed)[:10] if observed else '时点见来源')+marker+' · '+label,item['rows'],series);images.append(path)
        c=item['coverage']
        overview.append(['表覆盖',item['name'],c['completeness']+'；返回'+str(c['returned_group_count'])+'组；总体'+('未知' if c['population_group_count'] is None else str(c['population_group_count']))+'组'])
    overview.append(['状态','生成范围',doc.get('status','saved_observation')])
    overview.append(['边界','来源','各表保留原读取/ETL时间；DDP差额不是损失，源归一渠道不是责任归因'])
    sheets.insert(0,('管理层总览',['观察','指标','值'],overview,overview_formats))
    coverage_rows=[]
    for item in data:
        c=item['coverage'];coverage_rows.append([item['name'],c['status'],c['data_state'],c['completeness'],c['truncated'],c['returned_group_count'],c['population_group_count'] if c['population_group_count'] is not None else '未知',c['population_row_count'] if c['population_row_count'] is not None else '未知',c['observed_at'] or '未知',json.dumps(c.get('unknown_items') or {},ensure_ascii=False,sort_keys=True,default=str)])
    sheets.append(('覆盖证据',['表','status','data_state','completeness','truncated','返回分组数','总体分组数','总体行数','观察时点','未知项'],coverage_rows))
    if isinstance(doc.get('observations'),list):
        gate={'allowed':False,'reason_codes':['RAW_QUERY_EVIDENCE_MISSING'],'items':[]}
    elif isinstance(doc.get('query_packets'),list):
        gate=result_completeness.gate_for_document(doc)
    else:
        gate=result_completeness.report_delivery_gate([item['_result'] for item in data])
    sheets.append(('未验证事项',['项目','状态'],[['完整报告门槛','通过' if gate['allowed'] else '未通过：'+('、'.join(gate['reason_codes']) or '证据不足')],['正式责任归因','缺生产ETL/责任证据，不编造名单'],['品质/重复退货','生产规则未齐'],['完整递归血缘','未独立核验'],['截断与未知','各分项保留原查询证据；未知不填零']]))
    path=out/'Fabric_Source_Report.xlsx';gen_workbook_xlsx(sheets,path,borders=True,landscape=True)
    embed_coverage_markers(path,[f"truncated={str(item['coverage']['truncated']).lower()}; status={item['coverage']['status']}; completeness={item['coverage']['completeness']}" for item in data])
    if images:embed_charts(path,images,len(overview)+3)
    return [str(path),*[str(p) for p in images]]
