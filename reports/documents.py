# SPDX-License-Identifier: PolyForm-Strict-1.0.0
# Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed.
"""同一 manifest → DOCX → LibreOffice PDF，转换不持有业务事务锁。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from datetime import datetime
from docx import Document
from docx.shared import Mm, Pt, RGBColor
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from .domain import FIELDS, SECTIONS, SHANGHAI, as_item


def build_docx(manifest, path):
    document = Document()
    for element in list(document.styles.element.iter(qn('w:pBdr'))):
        element.getparent().remove(element)
    section = document.sections[0]
    section.page_width, section.page_height = Mm(210), Mm(297)
    section.top_margin, section.bottom_margin = Mm(22), Mm(20)
    section.left_margin = section.right_margin = Mm(22)
    for name in ['Normal', 'Title', 'Heading 1', 'Heading 2', 'Heading 3']:
        style = document.styles[name]
        style.font.name = 'Noto Sans CJK SC'
        style.element.get_or_add_rPr().rFonts.set(qn('w:eastAsia'), 'Noto Sans CJK SC')
        style.font.color.rgb = RGBColor.from_string('000000' if name in ('Normal', 'Title') else '20384D')
    document.styles['Normal'].font.size = Pt(11)
    document.styles['Normal'].paragraph_format.line_spacing = 1.3
    document.styles['Normal'].paragraph_format.space_after = Pt(4)
    document.styles['Title'].font.size = Pt(18)
    document.styles['Heading 1'].font.size = Pt(14)
    document.styles['Heading 2'].font.size = Pt(13)
    document.styles['Heading 3'].font.size = Pt(11)
    document.styles['Title'].paragraph_format.space_after = Pt(10)
    for name, before, after in [('Heading 1',12,6),('Heading 2',10,5),('Heading 3',7,3)]:
        paragraph = document.styles[name].paragraph_format
        paragraph.space_before, paragraph.space_after = Pt(before), Pt(after)
        paragraph.keep_with_next = True
    footer = section.footer.paragraphs[0]
    footer.alignment = 1
    footer.add_run('第 ')
    field = OxmlElement('w:fldSimple')
    field.set(qn('w:instr'), 'PAGE')
    footer._p.append(field)
    footer.add_run(' 页')
    originals = []
    for index, member in enumerate(manifest['members']):
        if not index:
            title = document.add_paragraph('课题组周报合订报告', 'Title')
            borders = OxmlElement('w:pBdr')
            bottom = OxmlElement('w:bottom')
            bottom.set(qn('w:val'), 'nil')
            borders.append(bottom)
            title._p.get_or_add_pPr().append(borders)
            generated = datetime.fromisoformat(manifest['created_at']).astimezone(SHANGHAI).strftime('%Y-%m-%d %H:%M（北京时间）')
            document.add_paragraph(f"{manifest['period']}\n生成时间 {generated}")
        member_title = document.add_paragraph(member['name'], 'Heading 1')
        if index:
            member_title.paragraph_format.page_break_before = True
        if member['returned']:
            document.add_paragraph('待修改，使用本周期上一正式版本')
        for section_key, section_label in SECTIONS.items():
            document.add_paragraph(section_label, 'Heading 2')
            for key, label in FIELDS.items():
                document.add_paragraph(label, 'Heading 3')
                item = member['payload'][section_key][key]
                if item['state'] == 'NONE':
                    document.add_paragraph('本周无／暂无')
                for number, raw in enumerate(item['items'], 1):
                    entry = as_item(raw)  # 第 2 版：要点 + 详细阐述；第 1 版旧正文只有一段原文
                    p = document.add_paragraph()
                    p.paragraph_format.left_indent = Mm(7)
                    p.paragraph_format.first_line_indent = Mm(-7)
                    p.add_run(f'{number}. ')
                    if entry['point']:
                        p.add_run(entry['point']).bold = True
                        p.paragraph_format.keep_with_next = bool(entry['detail'].strip())
                        originals.append((len(document.paragraphs) - 1, entry['point'], True))
                        if entry['detail'].strip():
                            detail = document.add_paragraph()
                            detail.paragraph_format.left_indent = Mm(7)
                            detail.add_run(entry['detail'])
                            originals.append((len(document.paragraphs) - 1, entry['detail'], False))
                    else:
                        p.add_run(entry['detail'])
                        originals.append((len(document.paragraphs) - 1, entry['detail'], True))
    for paragraph in document.paragraphs:
        for run in paragraph.runs:
            fonts = run._r.get_or_add_rPr().get_or_add_rFonts()
            fonts.attrib.clear()
            for attribute in ('ascii', 'hAnsi', 'eastAsia', 'cs'):
                fonts.set(qn(f'w:{attribute}'), 'Noto Sans CJK SC')
    document.save(path)
    reopened = Document(path)
    for index, text, numbered in originals:
        written = reopened.paragraphs[index].text
        assert (written.split('. ', 1)[1] if numbered else written) == text, 'DOCX 原文一致性失败'


def generate(manifest, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    docx = output / 'report.docx'
    build_docx(manifest, docx)
    binary = os.environ.get('LIBREOFFICE_BIN') or shutil.which('libreoffice') or shutil.which('soffice')
    if not binary:
        raise RuntimeError('找不到 LibreOffice；DOCX 已生成，PDF 转换未执行')
    with tempfile.TemporaryDirectory(prefix='weekly-lo-') as temporary:
        command = [binary, f'-env:UserInstallation={Path(temporary).as_uri()}', '--headless', '--convert-to', 'pdf', '--outdir', str(output.resolve()), str(docx.resolve())]
        process = subprocess.run(command, capture_output=True, timeout=180, check=False)
        if process.returncode or not (output / 'report.pdf').is_file():
            raise RuntimeError(f'LibreOffice 转换失败，退出码 {process.returncode}')
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
