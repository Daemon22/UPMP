from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "previews"
W, H = 1920, 1080
FONT_DIR = Path(r"C:\Windows\Fonts")


def font(name: str, size: int):
    candidates = {
        "sans": ["segoeui.ttf", "arial.ttf"],
        "medium": ["seguisb.ttf", "segoeuib.ttf", "arialbd.ttf"],
        "bold": ["segoeuib.ttf", "seguisb.ttf", "arialbd.ttf"],
        "serif": ["georgia.ttf", "times.ttf"],
        "serif_bold": ["georgiab.ttf", "timesbd.ttf"],
    }[name]
    for filename in candidates:
        path = FONT_DIR / filename
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


F = {k: font(k, s) for k, s in {"sans": 17, "medium": 18, "bold": 18, "serif": 27, "serif_bold": 29}.items()}
COL = {"bg": "#071725", "panel": "#0d2438", "panel2": "#112e47", "line": "#203e55", "text": "#eef7ff", "muted": "#8ca7bc", "gold": "#edc36e", "gold2": "#ffe7ad", "blue": "#35c8ff", "teal": "#45dfce", "green": "#77e4a7", "violet": "#bd95ff", "red": "#ff8b82", "waiting": "#8ebad6"}


def ft(name, size):
    return font(name, size)


def rounded(draw, box, radius, fill, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def text(draw, xy, value, size=12, fill=None, face="sans", anchor=None):
    scale = 1.28 if size < 20 else 1.12
    draw.text(xy, value, font=ft(face, round(size*scale)), fill=fill or COL["text"], anchor=anchor)


def wrapped(draw, xy, value, max_width, size=11, fill=None, face="sans", leading=None, max_lines=None):
    scale = 1.28 if size < 20 else 1.12
    size = round(size*scale)
    leading = round(leading*scale) if leading else None
    f = ft(face, size)
    words = value.split()
    lines, line = [], ""
    for word in words:
        test = (line + " " + word).strip()
        if draw.textlength(test, font=f) <= max_width or not line:
            line = test
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines]
        while lines[-1] and draw.textlength(lines[-1] + "…", font=f) > max_width:
            lines[-1] = lines[-1][:-1]
        lines[-1] += "…"
    y = xy[1]
    step = leading or round(size * 1.42)
    for line in lines:
        draw.text((xy[0], y), line, font=f, fill=fill or COL["text"])
        y += step
    return y


def make_background():
    im = Image.new("RGB", (W, H))
    px = im.load()
    top, bottom = (5, 19, 32), (7, 24, 40)
    for y in range(H):
        t = y / H
        c = tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3))
        for x in range(W):
            px[x, y] = c
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((810, -430, 1840, 500), fill=(28, 109, 165, 48))
    gd.ellipse((300, 540, 1110, 1320), fill=(35, 125, 109, 23))
    gd.ellipse((1440, 210, 2000, 860), fill=(92, 57, 142, 18))
    glow = glow.filter(ImageFilter.GaussianBlur(140))
    im = Image.alpha_composite(im.convert("RGBA"), glow).convert("RGB")
    d = ImageDraw.Draw(im, "RGBA")
    # topographic contours, deliberately quiet behind the process channels
    for cx, cy, base, count in [(820, 560, 110, 7), (1440, 240, 98, 6), (1140, 980, 85, 5)]:
        for i in range(count):
            r = base + i * 36
            d.ellipse((cx-r*1.55, cy-r*.72, cx+r*1.55, cy+r*.72), outline=(112, 166, 194, max(7, 22-i*2)), width=2)
    return im


def pill(draw, x, y, label, color, fill=(12, 31, 47, 235), border=None, size=9, pad_x=10, height=24):
    size = round(size*1.17)
    f = ft("bold", size)
    width = int(draw.textlength(label, font=f)) + 2 * pad_x
    rounded(draw, (x, y, x+width, y+height), height//2, fill, border or color, 1)
    draw.text((x+pad_x, y+(height-size)//2-1), label, font=f, fill=color)
    return width


def gradient_path(im, points, width=5, colors=None):
    colors = colors or [(46, 201, 255), (67, 225, 196), (86, 174, 255), (175, 126, 255), (240, 196, 106)]
    glow = Image.new("RGBA", im.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.line(points, fill=(73, 191, 232, 90), width=width*7, joint="curve")
    glow = glow.filter(ImageFilter.GaussianBlur(15))
    im.alpha_composite(glow)
    d = ImageDraw.Draw(im, "RGBA")
    d.line(points, fill=(7, 19, 31, 245), width=width*4+6, joint="curve")
    d.line(points, fill=(40, 134, 177, 80), width=width*4, joint="curve")
    # color-change along the channel gives the tributary a luminous, living edge
    for i in range(len(points)-1):
        t = i / max(1, len(points)-2)
        j = min(len(colors)-2, int(t*(len(colors)-1)))
        local = t*(len(colors)-1)-j
        c = tuple(int(colors[j][k]*(1-local)+colors[j+1][k]*local) for k in range(3))
        d.line([points[i], points[i+1]], fill=(*c, 245), width=width, joint="curve")
        if width > 2:
            d.line([points[i], points[i+1]], fill=(226, 250, 255, 132), width=1)


def bezier(p0, p1, p2, p3, steps=50):
    pts = []
    for i in range(steps+1):
        t = i/steps; u = 1-t
        x = u**3*p0[0] + 3*u*u*t*p1[0] + 3*u*t*t*p2[0] + t**3*p3[0]
        y = u**3*p0[1] + 3*u*u*t*p1[1] + 3*u*t*t*p2[1] + t**3*p3[1]
        pts.append((int(x), int(y)))
    return pts


def draw_brand(im, complex_mode=False):
    d = ImageDraw.Draw(im, "RGBA")
    d.rectangle((0, 0, 226, H), fill=(5, 18, 31, 240))
    d.line((225, 0, 225, H), fill=(35, 61, 80, 180), width=1)
    mark = Image.open(ROOT / "assets" / "brand-emblem.jpg").convert("RGB")
    mark.thumbnail((66, 66), Image.Resampling.LANCZOS)
    mask = Image.new("L", mark.size, 0)
    ImageDraw.Draw(mask).ellipse((0, 0, mark.width-1, mark.height-1), fill=255)
    d.ellipse((20, 19, 68, 67), fill=(7, 26, 41, 255), outline=(237,195,110,170), width=2)
    im.paste(mark.resize((44,44), Image.Resampling.LANCZOS), (22,21), mask.resize((44,44)))
    wordmark=Image.open(ROOT / "assets" / "brand-wordmark.png").convert("RGBA")
    wordmark.thumbnail((140,34),Image.Resampling.LANCZOS)
    im.alpha_composite(wordmark,(74,18))
    text(d, (82, 55), "PROCESS OBSERVATORY", 7, COL["gold2"], "bold")
    d.line((19, 82, 206, 82), fill=(39, 65, 84, 180), width=1)
    text(d, (31, 108), "WORKSPACE", 9, "#7792a9", "bold")
    nav = [("river", "Process river", True), ("activity", "Run activity", False), ("mutations", "Mutation ledger", False), ("outputs", "Output surfaces", False)]
    for i, (icon, label, active) in enumerate(nav):
        y = 137+i*48
        if active:
            rounded(d, (13,y,212,y+38), 10, (23,64,91,205), (44,113,151,140), 1)
            d.line((14,y+8,14,y+30), fill=COL["gold"], width=3)
        color=COL["gold"] if active else "#91c8e9"
        if icon=="river":
            for dy in (0,5,10):
                d.line([(28,y+11+dy),(33,y+11+dy-2),(38,y+11+dy),(43,y+11+dy+2)],fill=color,width=2)
        elif icon=="activity":
            d.line([(28,y+21),(32,y+21),(35,y+13),(40,y+29),(44,y+18),(49,y+18)],fill=color,width=2,joint="curve")
        elif icon=="mutations":
            d.line([(30,y+13),(30,y+28),(46,y+13),(46,y+28)],fill=color,width=1)
            d.line([(30,y+20),(46,y+20)],fill=color,width=1)
            for xx,yy in [(30,y+13),(30,y+28),(46,y+13),(46,y+28),(38,y+20)]:
                d.ellipse((xx-2,yy-2,xx+2,yy+2),fill=color)
        else:
            d.rounded_rectangle((28,y+13,36,y+21),radius=1,outline=color,width=1)
            d.rounded_rectangle((42,y+23,50,y+31),radius=1,outline=color,width=1)
            d.line([(36,y+17),(40,y+17),(40,y+27),(42,y+27)],fill=color,width=1)
        text(d, (58,y+12), label, 11, "#f2f8fc" if active else "#9bb1c4", "bold")
        if label in ("Run activity", "Mutation ledger"):
            rounded(d, (184,y+10,203,y+28), 6, (95,73,38,100), (185,145,70,70), 1)
            text(d, (193.5,y+19), "6" if label=="Run activity" else "1", 8, COL["gold2"], "bold", "mm")
    rounded(d, (14, 884, 211, 1004), 12, (15,42,61,245), (37,69,90,190), 1)
    text(d, (27, 901), "CURRENT RUN", 8, "#a4b7c5", "bold")
    d.ellipse((186,902,193,909), fill=COL["green"])
    text(d, (27, 925), "Process ecosystem" if complex_mode else "Identity assurance", 11, COL["text"], "bold")
    text(d, (27, 946), "SAMPLE · RUN 0429A" if complex_mode else "DEMO · RUN 0429A", 8, "#7793aa", "medium")
    d.line((27, 967, 198, 967), fill=(42,68,86,180), width=1)
    text(d, (27, 979), "Checkpoints passed" if complex_mode else "Evidence linked", 8, "#9bb0c1", "sans")
    text(d, (190, 979), "18 / 27" if complex_mode else "4 / 5", 9, COL["green"], "bold", "ra")
    text(d, (22, 1037), "MONITOR  ·  VERIFY  ·  ENSURE", 7, "#728ca1", "bold")


def root_content(level):
    root = level == "overview"
    button = level == "button-build"
    if root:
        return ("Authentication & identity assurance", "PROCESS RIVER · OVERVIEW", "Five core streams · sign-up and sign-in tributaries · one controlled outcome", "all stages · select a stream to inspect", [
            ("01", "Intent & scope", "Define the required identity result", "Succeeded", 100, "done"),
            ("02", "Identity context", "Establish the person and session", "Succeeded", 100, "done"),
            ("03", "Evidence capture", "Collect proof from the approved source", "Deviation", 68, "attention"),
            ("04", "Verify & decide", "Compare evidence against approved rules", "In progress", 46, "active"),
            ("05", "Outcome gate", "Release only an allowed result", "Waiting", 0, "waiting"),
        ], 2, "Evidence capture", "Collect proof from the approved source", 68,
                "One extra capture retry followed a permission change.", "2 / 3 passed", "Capture event · device permission · retry counter", "A verified identity is produced only after the required evidence checks pass.")
    if button:
        return ("Output origin · Face Verification button", "OUTPUT LINEAGE · ORIGIN TO INTERFACE", "Trace the intent, component, action binding, verification, and release", "selected output · upstream design and downstream invocation", [
            ("O.1", "Declare the intent", "Define the action and its purpose", "Succeeded", 100, "done"),
            ("O.2", "Compose the control", "Label, icon, and accessible states", "Succeeded", 100, "done"),
            ("O.3", "Bind the action", "Connect click to the approved request", "Succeeded", 100, "done"),
            ("O.4", "Verify behavior", "Check loading, error, retry, and hold", "In progress", 82, "active"),
            ("O.5", "Release surface", "Expose in allowed application flows", "Waiting", 0, "waiting"),
        ], 4, "Face Verification button", "Visible application control · connected to sign-up and sign-in flows", 82,
                "Release awaits behavior and accessibility checks.", "4 / 5 origin checks", "Action contract · surface links · behavior checks", "Activation opens one identity-bound verification request; the button never asserts a match by itself.")
    return ("Evidence capture & face verification", "FOCUSED STREAM · EVIDENCE TO DECISION", "Zoomed into verification · capture, quality, comparison, and guarded result", "verification sub-process · 5 checkpoints · output remains gated", [
        ("04.1", "Authorize device", "Confirm allowed capture source", "Succeeded", 100, "done"),
        ("04.2", "Capture sample", "Acquire live proof for this request", "Deviation", 68, "attention"),
        ("04.3", "Quality & reference", "Check sample and approved reference", "In progress", 54, "active"),
        ("04.4", "Compare evidence", "Apply the declared comparison rule", "Waiting", 0, "waiting"),
        ("04.5", "Decision & route", "Match, mismatch, or human review", "Waiting", 0, "waiting"),
    ], 1, "Capture sample", "Acquire a live sample and link it to the active verification request.", 68,
                "Capture retry count is one above the expected path. The event is retained and contained.", "2 / 3 passed", "Sample digest · capture time · retry event", "A supported result is emitted; uncertainty is held or escalated.")


def draw_topbar(im, title, complex_mode=False):
    d = ImageDraw.Draw(im, "RGBA")
    x0, x1 = 226, 1550
    d.line((x0, 64, x1, 64), fill=(35,61,80,140), width=1)
    crumb="Ecosystem" if complex_mode else "Overview"
    text(d, (249, 26), crumb, 9, "#8099ad", "medium")
    crumb_width=d.textlength(crumb,font=ft("medium",round(9*1.28)))
    arrow_x=249+int(crumb_width)+8
    text(d, (arrow_x, 26), "›", 13, "#536d82", "sans")
    text(d, (arrow_x+16, 26), title[:33], 9, "#eaf4fc", "bold")
    text(d, (1143, 26), "RUN 0429A  /  FULL ECOSYSTEM" if complex_mode else "RUN 0429A  /  IDENTITY ASSURANCE", 8, "#7c97ae", "medium")
    pill(d, 1434, 15, "SAMPLE RUN", "#a4edc5", (15,46,39,235), (40,102,73,180), 8, 10, 23)
    rounded(d, (1512,14,1539,42), 8, (19,39,55,230), (40,66,86,190), 1)
    text(d, (1525,28), "?", 13, "#a7c1d6", "bold", "mm")


def draw_metrics(im):
    d = ImageDraw.Draw(im, "RGBA")
    items=[("DESIRED OUTCOME", "Verified identity", "evidence required", COL["gold2"]), ("ACTUAL PROCESS PROGRESS", "68%", "4 of 6 checkpoints passed", COL["gold2"]), ("DEVIATION WATCH", "1 open", "retry path under review", COL["red"]), ("RUN INTEGRITY", "Contained", "outcome gate is holding", COL["green"])]
    for i,(label,val,note,accent) in enumerate(items):
        x=246+i*321; y=204
        rounded(d,(x,y,x+308,y+72),12,(15,39,58,225),(36,66,87,150),1)
        d.ellipse((x+267,y-22,x+325,y+36),outline=(237,195,110,22),width=2)
        text(d,(x+14,y+13),label,8,"#8ca6bb","bold")
        text(d,(x+14,y+36),val,16,accent,"serif_bold" if i==0 else "bold")
        value_face="serif_bold" if i==0 else "bold"
        value_font=ft(value_face,round(16*(1.28 if 16<20 else 1.12)))
        xnote=x+14+int(d.textlength(val,font=value_font))+12
        text(d,(xnote,y+42),note,8,"#84a0b4","sans")


def draw_screen(level, path):
    im=make_background().convert("RGBA")
    draw_brand(im)
    title,map_kicker,map_sub,context,nodes,selected,sel_title,sel_desc,progress,dev,checks,evidence,expected=root_content(level)
    draw_topbar(im,title)
    d=ImageDraw.Draw(im,"RGBA")
    text(d,(247,91),"UPMP  ·  UNIVERSAL PROCESS MONITORING PACKAGE",9,COL["gold"],"bold")
    text(d,(246,112),title,27,"#f6fbff","serif_bold")
    subtitle=("Follow the flow from its intended outcome through every branch, checkpoint, and connected result." if level=="overview" else "Open one focused stream at a time; step back to restore the parent process view." if level!="button-build" else "Open an output to see how it was created, what invokes it, and where the result connects next.")
    text(d,(248,153),subtitle,10,"#8fa9bf","sans")
    pill(d,1289,121,"ILLUSTRATIVE MONITORING DATA","#c0a76f",(37,33,25,185),(120,99,58,130),8,11,24)
    draw_metrics(im)
    # map window
    mx,my,mw,mh=246,294,1286,553
    rounded(d,(mx,my,mx+mw,my+mh),16,(11,32,49,247),(38,69,91,180),1)
    d.line((mx,my+57,mx+mw,my+57),fill=(34,61,80,190),width=1)
    rounded(d,(mx+15,my+13,mx+48,my+46),9,(29,75,95,185),(126,142,93,90),1)
    for dy in (0,5,10):
        d.line([(mx+21,my+25+dy),(mx+26,my+25+dy-2),(mx+31,my+25+dy),(mx+36,my+25+dy+2)],fill=COL["gold2"],width=2)
    text(d,(mx+60,my+15),map_kicker,9,COL["text"],"bold")
    text(d,(mx+60,my+33),map_sub,8,"#829db2","sans")
    # view tabs and zoom control
    pill(d,mx+866,my+17,"RIVER MAP","#eaf6ff",(36,82,111,185),(63,108,136,140),8,9,24)
    pill(d,mx+960,my+17,"ACTIVITY","#7892a6",(10,25,40,220),(40,60,77,170),8,9,24)
    pill(d,mx+1046,my+17,"MUTATIONS","#7892a6",(10,25,40,220),(40,60,77,170),8,9,24)
    rounded(d,(mx+1162,my+15,mx+1269,my+43),8,(8,24,39,210),(38,63,81,170),1)
    text(d,(mx+1176,my+29),"−",14,"#a9bfd0","sans","mm"); text(d,(mx+1215,my+29),"100%",8,"#819cb1","bold","mm");text(d,(mx+1255,my+29),"+",13,"#a9bfd0","sans","mm")
    text(d,(mx+16,my+71),"VIEW:  "+context,8,"#89a5ba","medium")
    for i,(lab,c) in enumerate([("ALL","#d6efff"),("ACTIVE","#ffc86c"),("DEVIATIONS","#ff9b93")]):
        pill(d,mx+1000+i*78,my+63,lab,c,(22,53,71,190) if i==0 else (9,26,41,220),(57,87,104,130),7,7,20)
    # canvas with faint lattice and ambient glow
    cx,cy,cw,ch=mx+9,my+94,mw-18,403
    d.rounded_rectangle((cx,cy,cx+cw,cy+ch),radius=12,fill=(5,20,34,155))
    # low contrast topographic ellipses
    for i in range(8):
        d.ellipse((cx+265-i*17,cy+98-i*13,cx+780+i*34,cy+330+i*16),outline=(86,147,180,max(6,15-i)),width=1)
    centers=[cx+int(cw*f) for f in [.10,.30,.50,.70,.90]]
    base_y=cy+200
    if level=="overview":
        # broad confluence from entry context into shared verification
        gradient_path(im,bezier((centers[0]+64,base_y),(centers[0]+116,base_y),(centers[1]-105,base_y),(centers[1],base_y),40),5)
        gradient_path(im,bezier((centers[1],base_y),(centers[1]+96,base_y),(centers[2]-102,base_y),(centers[2],base_y),45),5)
        gradient_path(im,bezier((centers[2],base_y),(centers[2]+92,base_y),(centers[3]-108,base_y),(centers[3],base_y),45),5)
        gradient_path(im,bezier((centers[3],base_y),(centers[3]+94,base_y),(centers[4]-100,base_y),(centers[4],base_y),45),5)
        for yy,col in [(cy+84,(53,226,176)),(cy+319,(183,141,255))]:
            pts=bezier((centers[1],base_y),(centers[1]+82,base_y),(centers[1]+64,yy),(centers[1]+112,yy),35)+bezier((centers[1]+112,yy),(centers[1]+278,yy),(centers[3]-138,yy),(centers[3]-62,base_y),55)[1:]
            gradient_path(im,pts,3,[col,(55,200,245),(177,130,255)])
        pill(d,centers[1]+69,cy+70,"SIGN-UP", "#91f0bd",(7,25,39,238),(62,130,102,150),8,9,21)
        pill(d,centers[1]+69,cy+299,"SIGN-IN", "#d3b4ff",(7,25,39,238),(119,89,154,150),8,9,21)
        pill(d,centers[2]+32,cy+185,"MERGE · SHARED PROOF",COL["gold2"],(7,25,39,238),(143,113,59,150),7,8,20)
    elif level=="face-check":
        for i in range(4):
            gradient_path(im,bezier((centers[i]+64,base_y),(centers[i]+110,base_y),(centers[i+1]-112,base_y),(centers[i+1]-64,base_y),36),4)
        top=bezier((centers[1],base_y),(centers[1]+80,base_y),(centers[1]+54,cy+102),(centers[2],cy+102),36)+bezier((centers[2],cy+102),(centers[2]+92,cy+102),(centers[2]+42,base_y),(centers[2]+70,base_y),36)[1:]
        bot=bezier((centers[1],base_y),(centers[1]+80,base_y),(centers[1]+54,cy+306),(centers[2],cy+306),36)+bezier((centers[2],cy+306),(centers[2]+92,cy+306),(centers[2]+42,base_y),(centers[2]+70,base_y),36)[1:]
        gradient_path(im,top,3,[ (51,221,177),(65,193,255),(165,132,255)])
        gradient_path(im,bot,3,[ (174,131,255),(74,178,255),(72,222,176)])
        pill(d,centers[2]-55,cy+55,"SAMPLE QUALITY", "#92efbe",(7,25,39,238),(62,130,102,150),7,8,20)
        pill(d,centers[2]-77,cy+322,"REFERENCE PROVENANCE", "#d3b4ff",(7,25,39,238),(119,89,154,150),7,8,20)
        pill(d,centers[3]-32,cy+186,"BOTH MUST PASS",COL["gold2"],(7,25,39,238),(143,113,59,150),7,8,20)
    else:
        for i in range(4):
            gradient_path(im,bezier((centers[i]+63,base_y),(centers[i]+100,base_y),(centers[i+1]-110,base_y),(centers[i+1]-63,base_y),34),5)
        pts=bezier((centers[1],base_y),(centers[1]+76,base_y),(centers[1]+65,cy+118),(centers[2],cy+118),35)+bezier((centers[2],cy+118),(centers[2]+92,cy+118),(centers[2]+50,base_y),(centers[2]+64,base_y),35)[1:]
        gradient_path(im,pts,3,[(61,221,177),(61,192,255),(171,132,255)])
        pill(d,centers[2]-72,cy+77,"ACCESSIBILITY & STATE", "#91efbd",(7,25,39,238),(62,130,102,150),7,8,20)
        pill(d,centers[3]-56,cy+186,"CONTROLLED RELEASE",COL["gold2"],(7,25,39,238),(143,113,59,150),7,8,20)
    # node cards
    anchor_ids=[]
    for i,node in enumerate(nodes):
        nn,heading,desc,status,pct,state=node
        x=centers[i]; y=base_y; w,h=190,118; box=(x-w//2,y-h//2,x+w//2,y+h//2)
        if i==selected:
            halo=Image.new("RGBA",im.size,(0,0,0,0)); hd=ImageDraw.Draw(halo)
            hd.rounded_rectangle((box[0]-6,box[1]-6,box[2]+6,box[3]+6),radius=18,outline=(237,195,110,115),width=5)
            im.alpha_composite(halo.filter(ImageFilter.GaussianBlur(11)))
        nd=ImageDraw.Draw(im,"RGBA")
        fill=(17,44,64,248) if i==selected else (10,30,47,247)
        outline=(255,220,151,220) if i==selected else (86,127,153,135)
        rounded(nd,box,13,fill,outline,2 if i==selected else 1)
        text(nd,(box[0]+13,box[1]+11),nn,9,COL["gold"],"serif_bold")
        status_color=COL["green"] if state=="done" else COL["red"] if state=="attention" else COL["gold2"] if state=="active" else COL["waiting"]
        pill(nd,box[2]-85,box[1]+8,status.upper(),status_color,(7,23,36,240),(66,91,106,160),7,7,18)
        wrapped(nd,(box[0]+13,box[1]+35),heading,160,10,COL["text"],"bold",13,2)
        wrapped(nd,(box[0]+13,box[1]+60),desc,160,8,"#91a9bc","sans",11,2)
        nd.rounded_rectangle((box[0]+13,box[3]-24,box[2]-13,box[3]-20),radius=3,fill=(255,255,255,25))
        prog_color=(83,222,203) if state!="attention" else (255,141,121)
        nd.rounded_rectangle((box[0]+13,box[3]-24,box[0]+13+int(164*pct/100),box[3]-20),radius=3,fill=prog_color)
        text(nd,(box[0]+13,box[3]-16),"ACTUAL / EXPECTED",7,"#6f8ba0","bold")
        text(nd,(box[2]-13,box[3]-16),f"{pct}%",8,"#d0e5f4","bold","ra")
    if level=="overview":
        # jewel output at the head of the outcome channel
        ox=cx+cw-10; oy=base_y
        d=ImageDraw.Draw(im,"RGBA")
        d.ellipse((ox-22,oy-22,ox+22,oy+22),fill=(16,86,141,255),outline=(255,220,135,230),width=2)
        d.ellipse((ox-14,oy-14,ox+14,oy+14),outline=(83,225,255,160),width=2)
        d.polygon([(ox,oy-11),(ox+4,oy-4),(ox+11,oy),(ox+4,oy+4),(ox,oy+11),(ox-4,oy+4),(ox-11,oy),(ox-4,oy-4)],fill="#fff0b9")
    # canvas legend
    text(d,(cx+16,cy+ch-20),"●  Succeeded",7,COL["green"],"medium")
    text(d,(cx+126,cy+ch-20),"●  In progress",7,COL["gold2"],"medium")
    text(d,(cx+259,cy+ch-20),"●  Deviation",7,COL["red"],"medium")
    text(d,(cx+370,cy+ch-20),"●  Waiting / gated",7,COL["waiting"],"medium")
    text(d,(cx+cw-270,cy+ch-20),"Click to inspect  ·  Enter stream to reveal children",7,"#718ba0","sans")
    # bottom assurance cards
    rounded(d,(246,861,912,979),12,(12,34,51,235),(37,67,86,170),1)
    text(d,(263,878),"✦  GOAL GUARD",8,COL["gold"],"bold")
    wrapped(d,(263,899),"The process can only release Verified when the required evidence and decision checks pass. Missing or conflicting proof keeps the result on hold.",622,10,"#c3d1dc","sans",15,4)
    rounded(d,(925,861,1532,979),12,(12,34,51,235),(37,67,86,170),1)
    text(d,(942,878),"✦  OUTPUT TRACEABILITY",8,COL["gold"],"bold")
    wrapped(d,(942,898),"Choose Face Verification to trace its design, action binding, invoking flow, and downstream connections.",565,9,"#c3d1dc","sans",13,3)
    pill(d,942,939,"TRACE FACE VERIFICATION BUTTON","#9adfff",(18,50,72,200),(51,114,146,130),7,9,22)
    # inspector panel
    ix=1550; d.rectangle((ix,0,W,H),fill=(6,20,33,235)); d.line((ix,0,ix,H),fill=(34,61,80,170),width=1)
    text(d,(1572,25),"OUTPUT ARTIFACT · TRACE O.5" if level=="button-build" else "SELECTED STREAM · "+nodes[selected][0],8,"#93aabd","bold")
    d.polygon([(1892,18),(1897,26),(1892,34),(1887,26)],fill=COL["gold"])
    shown_title="Face Verification button" if level=="button-build" else sel_title
    shown_sub="Visible application control · connected to sign-up and sign-in flows" if level=="button-build" else sel_desc+" · "+("top-level process" if level=="overview" else "verification sub-process")
    wrapped(d,(1572,58),shown_title,310,20,COL["text"],"serif_bold",24,2)
    wrapped(d,(1572,112),shown_sub,310,9,"#8da6b8","sans",13,3)
    state=nodes[selected][5]
    chip_color=COL["red"] if state=="attention" else COL["gold2"] if state=="active" else COL["green"] if state=="done" else COL["waiting"]
    pill(d,1572,157,"RELEASE PENDING" if level=="button-build" else nodes[selected][3].upper(),chip_color,(20,38,48,220),(74,95,105,120),8,9,22)
    pill(d,1686,157,checks, "#b5ccda",(16,34,49,220),(55,79,96,120),8,9,22)
    expected_text="Activation opens one identity-bound verification request; the button never asserts a match by itself." if level=="button-build" else expected
    rounded(d,(1572,194,1896,280),10,(46,39,25,110),(125,99,56,90),1)
    text(d,(1586,207),"ACTION CONTRACT" if level=="button-build" else "EXPECTED BEHAVIOR",7,"#b19a6e","bold")
    wrapped(d,(1586,225),expected_text,292,9,"#ffe5aa","serif_bold",13,3)
    text(d,(1572,293),"Origin workflow readiness" if level=="button-build" else "Observed progress · actual",8,"#8ea6b8","sans")
    shown_progress=82 if level=="button-build" else progress
    text(d,(1894,293),f"{shown_progress}%",10,COL["text"],"bold","ra")
    d.rounded_rectangle((1572,310,1896,316),radius=3,fill=(255,255,255,23))
    d.rounded_rectangle((1572,310,1572+int(324*shown_progress/100),316),radius=3,fill=(53,192,219,235))
    kv=([("Built from","Intent → component → action binding"),("Appears in","Sign-up · Sign-in"),("Invokes","Face verification process"),("Returns","Pending · verified · held")] if level=="button-build" else [("Checkpoint result",checks),("Run status",nodes[selected][3]),("Evidence mapped","Capture · permission · retry" if level=="overview" else evidence[:37]),("Mutation record","Linked to deviation" if state=="attention" else "No variance" if state=="done" else "Watch active")])
    y=335
    for a,b in kv:
        d.line((1572,y-5,1896,y-5),fill=(37,59,75,110),width=1)
        text(d,(1572,y+2),a,8,"#8299ac","sans")
        wrapped(d,(1723,y+2),b,171,8,COL["green"] if b in ("No variance",) else (COL["red"] if b=="Linked to deviation" else "#d4e4ee"),"bold",10,2)
        y+=34
    text(d,(1572,485),"CONNECTIONS & STATE" if level=="button-build" else "EVIDENCE & DEVIATIONS",8,"#7e9aaf","bold")
    if level=="button-build":
        for yy,mark,head,detail in [(508,"+","Two entry flows converge","Sign-up and sign-in share one request contract"),(547,"◇","Invokes evidence workflow","Capture → quality → comparison → guarded result"),(586,"II","Returns the observed state","Pending · verified · held · review required")]:
            d.ellipse((1573,yy,1591,yy+18),fill=(26,66,87,220),outline=(66,140,170,130),width=1)
            text(d,(1582,yy+9),mark,8,"#98dcf5","bold","mm")
            text(d,(1601,yy),head,8,"#e3edf3","bold")
            text(d,(1601,yy+16),detail,7,"#8ba4b7","sans")
        rounded(d,(1572,630,1896,684),9,(28,54,45,110),(72,133,94,110),1)
        text(d,(1584,640),"RELEASE GUARD",7,"#a8efc5","bold")
        wrapped(d,(1584,654),"No supported result, no success state.",295,8,"#d4e4db","sans",10,2)
    else:
        wrapped(d,(1573,508),evidence,313,8,"#d3e3ee","bold",11,2)
        text(d,(1573,536),"Observed for this sample run",7,"#7791a5","sans")
        rounded(d,(1572,559,1896,647),10,(65,38,36,115),(147,77,67,120),1)
        text(d,(1584,571),"!  DEVIATION WATCH",8,"#ffb09a","bold")
        wrapped(d,(1584,593),dev,296,8,"#d1c1b8","sans",12,3)
        text(d,(1584,631),"GOAL GUARD ACTIVE",7,"#ffce96","bold")
    # actions
    rounded(d,(1572,670,1896,708),9,(211,169,84,255),(244,212,149,180),1)
    text(d,(1734,689),"View origin steps" if level=="button-build" else "Enter this process",9,"#212831","bold","mm")
    rounded(d,(1572,718,1896,755),9,(15,51,73,210),(56,125,159,145),1)
    text(d,(1734,736),"Open invoking process" if level=="button-build" else "Trace Face Verification button",8,"#b2e9ff","bold","mm")
    wrapped(d,(1572,787),"Illustrative data only. A deployed view receives observed progress and evidence from instrumented events.",312,7,"#59748a","sans",11,4)
    # file footer, subtle edition marker
    text(d,(1518,1053),"UPMP  ·  PROPOSED OPERATOR INTERFACE  ·  ILLUSTRATIVE SAMPLE RUN",7,"#648098","bold","ra")
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    im.convert("RGB").save(path,format="PNG",optimize=True)


def draw_complex_metrics(im):
    d=ImageDraw.Draw(im,"RGBA")
    items=[("DESIRED OUTCOME","Supported outcome","Every required child evidenced",COL["gold2"]),("OBSERVED PROGRESS","68%","18 of 27 checkpoints passed",COL["gold2"]),("DEVIATION WATCH","2 mapped","1 open · 1 contained",COL["red"]),("RUN INTEGRITY","Mutation guarded","Unmapped changes: 0",COL["green"])]
    for i,(label,val,note,accent) in enumerate(items):
        x=246+i*321;y=195
        rounded(d,(x,y,x+308,y+72),12,(15,39,58,225),(36,66,87,150),1)
        d.ellipse((x+267,y-22,x+325,y+36),outline=(237,195,110,22),width=2)
        text(d,(x+14,y+13),label,8,"#8ca6bb","bold")
        text(d,(x+14,y+34),val,13,accent,"serif_bold" if i==0 else "bold")
        wrapped(d,(x+14,y+56),note,278,6,"#84a0b4","sans",8,1)


def draw_complex_header(im,title,subtitle,kicker="SYSTEM ECOSYSTEM · PROGRESSIVE PROCESS MAP"):
    draw_brand(im,True)
    draw_topbar(im,title,True)
    d=ImageDraw.Draw(im,"RGBA")
    text(d,(247,91),"UPMP  ·  UNIVERSAL PROCESS MONITORING PACKAGE",9,COL["gold"],"bold")
    text(d,(246,112),title,26,"#f6fbff","serif_bold")
    wrapped(d,(248,151),subtitle,960,9,"#8fa9bf","sans",13,2)
    pill(d,1289,121,"ILLUSTRATIVE SYSTEM MODEL","#c0a76f",(37,33,25,185),(120,99,58,130),8,11,24)
    draw_complex_metrics(im)


def draw_flow_card(im, box, n, heading, desc, status, pct, actual, selected=False, state="done"):
    d=ImageDraw.Draw(im,"RGBA")
    if selected:
        halo=Image.new("RGBA",im.size,(0,0,0,0));hd=ImageDraw.Draw(halo)
        hd.rounded_rectangle((box[0]-7,box[1]-7,box[2]+7,box[3]+7),radius=19,outline=(237,195,110,115),width=5)
        im.alpha_composite(halo.filter(ImageFilter.GaussianBlur(12)))
    fill=(17,44,64,250) if selected else (9,29,47,248)
    outline=(255,220,151,220) if selected else (86,127,153,155)
    rounded(d,box,13,fill,outline,2 if selected else 1)
    text(d,(box[0]+11,box[1]+10),n,8,COL["gold"],"serif_bold")
    status_color=COL["green"] if state=="done" else COL["red"] if state in ("attention","failed") else COL["gold2"] if state in ("active","guarded") else COL["waiting"]
    pill(d,box[2]-83,box[1]+8,status.upper(),status_color,(7,23,36,240),(66,91,106,160),6,6,17)
    wrapped(d,(box[0]+11,box[1]+34),heading,box[2]-box[0]-22,9,COL["text"],"bold",11,2)
    wrapped(d,(box[0]+11,box[1]+61),desc,box[2]-box[0]-22,7,"#91a9bc","sans",9,2)
    d.rounded_rectangle((box[0]+11,box[3]-24,box[2]-11,box[3]-20),radius=3,fill=(255,255,255,25))
    bar=COL["red"] if state in ("attention","failed") else COL["teal"]
    d.rounded_rectangle((box[0]+11,box[3]-24,box[0]+11+int((box[2]-box[0]-22)*pct/100),box[3]-20),radius=3,fill=bar)
    text(d,(box[0]+11,box[3]-16),"ACTUAL / EXPECTED",6,"#6f8ba0","bold")
    text(d,(box[2]-11,box[3]-16),actual,7,"#d0e5f4","bold","ra")


def draw_complex_overview(path):
    im=make_background().convert("RGBA")
    draw_complex_header(im,"UPMP · the living process ecosystem","A process-scale view: request and identity tributaries feed orchestration; parallel work streams send evidence into assurance, then guarded results return to APIs and screens.")
    d=ImageDraw.Draw(im,"RGBA")
    mx,my,mw,mh=246,286,1286,560
    rounded(d,(mx,my,mx+mw,my+mh),16,(11,32,49,247),(38,69,91,180),1)
    d.line((mx,my+57,mx+mw,my+57),fill=(34,61,80,190),width=1)
    rounded(d,(mx+15,my+13,mx+48,my+46),9,(29,75,95,185),(126,142,93,90),1)
    for dy in (0,5,10):d.line([(mx+21,my+25+dy),(mx+26,my+25+dy-2),(mx+31,my+25+dy),(mx+36,my+25+dy+2)],fill=COL["gold2"],width=2)
    text(d,(mx+60,my+15),"PROCESS RIVER · FULL ECOSYSTEM",9,COL["text"],"bold")
    text(d,(mx+60,my+33),"Six subsystems · parallel tributaries · shared evidence and outcome guard",8,"#829db2","sans")
    pill(d,mx+851,my+17,"RIVER MAP","#eaf6ff",(36,82,111,185),(63,108,136,140),8,9,24)
    pill(d,mx+946,my+17,"ARCHITECTURE","#b6d9ec",(10,25,40,220),(40,60,77,170),8,9,24)
    pill(d,mx+1070,my+17,"ACTIVITY","#7892a6",(10,25,40,220),(40,60,77,170),8,9,24)
    pill(d,mx+1160,my+17,"MUTATIONS","#7892a6",(10,25,40,220),(40,60,77,170),8,9,24)
    text(d,(mx+16,my+71),"VIEW:  System-level stream map · enter a subsystem to expose its branches",8,"#89a5ba","medium")
    for i,(lab,c) in enumerate([("ALL","#d6efff"),("ACTIVE","#ffc86c"),("DEVIATIONS","#ff9b93")]):pill(d,mx+1000+i*78,my+63,lab,c,(22,53,71,190) if i==0 else (9,26,41,220),(57,87,104,130),7,7,20)
    cx,cy,cw,ch=mx+9,my+94,mw-18,410
    d.rounded_rectangle((cx,cy,cx+cw,cy+ch),radius=12,fill=(5,20,34,155))
    for i in range(8):d.ellipse((cx+260-i*16,cy+90-i*12,cx+830+i*26,cy+330+i*16),outline=(86,147,180,max(6,15-i)),width=1)
    centers=[cx+int(cw*f) for f in [.08,.25,.42,.59,.76,.92]]
    base=cy+205
    for i in range(5):gradient_path(im,bezier((centers[i]+80,base),(centers[i]+113,base),(centers[i+1]-112,base),(centers[i+1]-80,base),38),4)
    branches=[(1,cy+91,[(55,221,177),(62,192,255),(171,132,255)]),(1,cy+321,[(174,131,255),(74,178,255),(72,222,176)]),(2,cy+53,[(58,221,177),(58,191,255),(173,132,255)]),(2,cy+357,[(176,132,255),(67,183,255),(72,222,176)]),(3,cy+122,[(58,221,177),(63,191,255),(172,132,255)]),(3,cy+293,[(176,132,255),(70,183,255),(71,221,176)])]
    for idx,yy,cols in branches:
        left=centers[idx];right=centers[min(idx+2,5)]
        pts=bezier((left,base),(left+52,base),(left+28,yy),(left+93,yy),34)+bezier((left+93,yy),(right-left-120,yy),(right-80,yy),(right-59,base),40)[1:]
        gradient_path(im,pts,2,cols)
    for x,y,label,color in [(centers[1]+34,cy+45,"SIGN-UP · NEW REQUEST",COL["green"]),(centers[1]+38,cy+367,"SIGN-IN · CONTINUING RUN",COL["violet"]),(centers[2]+18,cy+30,"INTEGRATIONS",COL["green"]),(centers[2]+18,cy+374,"BACKGROUND TASKS",COL["violet"]),(centers[3]+35,cy+93,"EVIDENCE CONFLUENCE",COL["gold2"]),(centers[4]+18,cy+175,"GUARDED RELEASE",COL["gold2"])]:pill(d,x,y,label,color,(7,25,39,238),(62,130,102,150),6,8,19)
    nodes=[("01","Intent & entry","Goals · requests · triggers","Succeeded",100,"3 / 3","done"),("02","Orchestration","Plan · schedule · recover","In progress",86,"6 / 7","active"),("03","Parallel workstreams","Identity · services · APIs","Deviation",72,"8 / 11","attention"),("04","Evidence fabric","Events · sources · custody","In progress",65,"5 / 8","active"),("05","Assurance & decision","Drift · policy · goal guard","Guarded",44,"3 / 7","guarded"),("06","Outcomes & surfaces","API · controls · audit","Waiting",25,"1 / 4","waiting")]
    for i,node in enumerate(nodes):
        x=centers[i];w=178;h=126;box=(x-w//2,base-h//2,x+w//2,base+h//2)
        draw_flow_card(im,box,*node[:6],selected=i==4,state=node[6])
    # jewel at the downstream end of the outcome channel
    d=ImageDraw.Draw(im,"RGBA");ox=cx+cw-8;oy=base
    d.ellipse((ox-21,oy-21,ox+21,oy+21),fill=(16,86,141,255),outline=(255,220,135,230),width=2)
    d.ellipse((ox-14,oy-14,ox+14,oy+14),outline=(83,225,255,160),width=2)
    d.polygon([(ox,oy-10),(ox+4,oy-4),(ox+10,oy),(ox+4,oy+4),(ox,oy+10),(ox-4,oy+4),(ox-10,oy),(ox-4,oy-4)],fill="#fff0b9")
    text(d,(cx+16,cy+ch-21),"●  Succeeded",7,COL["green"],"medium");text(d,(cx+132,cy+ch-21),"●  In progress",7,COL["gold2"],"medium");text(d,(cx+270,cy+ch-21),"●  Deviation / failed",7,COL["red"],"medium");text(d,(cx+443,cy+ch-21),"●  Waiting / gated",7,COL["waiting"],"medium")
    text(d,(cx+cw-323,cy+ch-21),"Enter a stream to reveal its child processes",7,"#718ba0","sans")
    rounded(d,(246,861,912,979),12,(12,34,51,235),(37,67,86,170),1)
    text(d,(263,878),"✦  EVERY CHILD REPORTS ITS OWN TRUTH",8,COL["gold"],"bold")
    wrapped(d,(263,899),"Progress is calculated from observed checkpoints and terminal receipts. Running, waiting, failed, and held branches stay distinct in the parent run.",622,9,"#c3d1dc","sans",13,4)
    rounded(d,(925,861,1532,979),12,(12,34,51,235),(37,67,86,170),1)
    text(d,(942,878),"✦  NO UNSUPPORTED SUCCESS",8,COL["gold"],"bold")
    wrapped(d,(942,898),"Two provenance gaps and one schema mutation hold the result. Unknown is shown as unknown; dependent outputs cannot pretend the goal was achieved.",565,9,"#c3d1dc","sans",13,4)
    draw_complex_inspector(im,"SYSTEM RUN · 0429A","The living process ecosystem","Six major subsystems · twenty-seven sample checkpoints · one shared outcome guard.","68%","18 of 27","2 mapped deviations","One integration reply is quarantined; two evidence origins remain unresolved.")
    im.convert("RGB").save(path,format="PNG",optimize=True)


def draw_complex_inspector(im,label,title,subtitle,progress,passed,deviations,watch):
    d=ImageDraw.Draw(im,"RGBA");ix=1550
    d.rectangle((ix,0,1920,1080),fill=(6,20,33,242));d.line((ix,0,ix,1080),fill=(34,61,80,170),width=1)
    text(d,(1572,25),label,8,"#93aabd","bold")
    d.polygon([(1892,18),(1897,26),(1892,34),(1887,26)],fill=COL["gold"])
    wrapped(d,(1572,58),title,310,18,COL["text"],"serif_bold",23,3)
    wrapped(d,(1572,130),subtitle,310,8,"#8da6b8","sans",12,4)
    pill(d,1572,190,"ILLUSTRATIVE RUN","#ffe2a4",(30,42,45,220),(84,101,94,120),7,8,20)
    pill(d,1710,190,"GOAL GUARDED","#ffce96",(52,38,30,220),(117,83,52,120),7,8,20)
    rounded(d,(1572,228,1896,316),10,(46,39,25,110),(125,99,56,90),1)
    if progress=="—":
        text(d,(1586,241),"REFERENCE SERVICE CATALOG",7,"#b19a6e","bold")
        text(d,(1890,265),"4 planes",10,COL["text"],"bold","ra")
        d.line((1586,282,1882,282),fill=(77,121,145,130),width=2)
        text(d,(1586,296),"24 connected service cards",7,"#d4e4ee","sans")
    else:
        text(d,(1586,241),"OBSERVED END-TO-END PROGRESS",7,"#b19a6e","bold")
        text(d,(1890,265),progress,12,COL["text"],"bold","ra")
        progress_value=int(progress.strip("%")) if progress.endswith("%") else 0
        d.rounded_rectangle((1586,280,1882,286),radius=3,fill=(255,255,255,23));d.rounded_rectangle((1586,280,1586+int(296*progress_value/100),286),radius=3,fill=(53,192,219,235))
        text(d,(1586,296),passed+" checkpoints passed",7,"#d4e4ee","sans")
    kv=[("Completed subsystems","2 of 6"),("Deviation watch",deviations),("Running branches","6 active"),("Mutation source","One change unknown"),("Release state","Held · no false success")]
    y=342
    for a,b in kv:
        d.line((1572,y-5,1896,y-5),fill=(37,59,75,110),width=1)
        text(d,(1572,y+2),a,7,"#8299ac","sans")
        wrapped(d,(1727,y+2),b,165,7,COL["red"] if ("unknown" in b.lower() or "held" in b.lower()) else "#d4e4ee","bold",9,2)
        y+=32
    text(d,(1572,515),"CROSS-SYSTEM WATCH",8,"#ffb09a","bold")
    rounded(d,(1572,538,1896,650),9,(65,38,36,115),(147,77,67,120),1)
    wrapped(d,(1584,553),watch,296,8,"#d1c1b8","sans",12,5)
    text(d,(1584,620),"DOWNSTREAM RELEASE: HELD",7,"#ffce96","bold")
    rounded(d,(1572,670,1896,709),9,(211,169,84,255),(244,212,149,180),1)
    text(d,(1734,689),"Open orchestration lane",8,"#212831","bold","mm")
    rounded(d,(1572,718,1896,757),9,(15,51,73,210),(56,125,159,145),1)
    text(d,(1734,737),"Trace outcome surfaces",8,"#b2e9ff","bold","mm")
    wrapped(d,(1572,785),"Illustrative data only. A deployed view would receive actual state, evidence, deviations, and mutation links from instrumented process events.",312,7,"#59748a","sans",11,5)


def draw_architecture(path):
    im=make_background().convert("RGBA")
    draw_complex_header(im,"UPMP system architecture","Four cooperating planes weave process execution, event evidence, and outcome assurance into one traceable run.","REFERENCE ARCHITECTURE · SERVICES AND SHARED PLANES")
    d=ImageDraw.Draw(im,"RGBA");mx,my,mw,mh=246,286,1286,694
    rounded(d,(mx,my,mx+mw,my+mh),16,(11,32,49,247),(38,69,91,180),1)
    d.line((mx,my+57,mx+mw,my+57),fill=(34,61,80,190),width=1)
    rounded(d,(mx+15,my+13,mx+48,my+46),9,(29,75,95,185),(126,142,93,90),1)
    for dy in (0,5,10):d.line([(mx+21,my+25+dy),(mx+26,my+25+dy-2),(mx+31,my+25+dy),(mx+36,my+25+dy+2)],fill=COL["gold2"],width=2)
    text(d,(mx+60,my+15),"ARCHITECTURE · FOUR PLANES, ONE TRACE",9,COL["text"],"bold")
    text(d,(mx+60,my+33),"Select a service to enter its process · shared run ID binds every lane",8,"#829db2","sans")
    pill(d,mx+1046,my+17,"REFERENCE DESIGN","#ffe2a4",(37,33,25,185),(120,99,58,130),7,10,23)
    groups=[
      ("01 · ORCHESTRATION CONTROL PLANE","intent · plan · schedule · join · recover",["Goal & process registry","Run coordinator","Dependency scheduler","Retry & recovery","Baseline & versioning","Run ledger"],["Outcome contracts","Branches and joins","Parallel child runs","Bounded retries","Expected graph","Status and receipts"],["#edc36e","#45c9ff","#45dfc1","#bd92ff","#ffd27c","#65dcff"]),
      ("02 · EXECUTION WORKSTREAMS","independent task services · scoped integrations · explicit receipts",["Identity & session","Capture & inputs","Domain services","External integrations","Decision & policy","Output surfaces"],["Actor and consent","Approved sources","Task processing","Validate replies","Rules and reasons","API, UI, controls"],["#4cdbb0","#64c9ff","#ffd27c","#c391ff","#5fdfe0","#ffcf75"]),
      ("03 · EVIDENCE & EVENT FABRIC","append-only events · artifacts · provenance · correlated outcomes",["Event spine","Artifact registry","Provenance chain","Validation service","Correlation fabric","Receipt & audit"],["Run transitions","Input fingerprints","Source and custody","Schema and quality","Run IDs across lanes","Terminal evidence"],["#62d7ff","#49dfc0","#c291ff","#70d4ff","#ffd27c","#62d7ff"]),
      ("04 · ASSURANCE & GOAL GUARD","compare expected and actual · contain uncertainty · explain every route",["Expected vs actual","Deviation monitor","Mutation ledger","Policy guard","Replay & diagnosis","Human review"],["Graph differences","Severity and impact","Origin and diff","Pass, hold, fail","Rebuild timeline","Escalation with context"],["#ffd27c","#ff9b91","#c291ff","#56dfcc","#65caff","#ffb69a"])
    ]
    row_ys=[367,510,653,796]
    # shared correlation filaments appear behind the cards, linking all planes
    for i,x in enumerate([417,605,793,981,1169,1357]):
        pts=[]
        for yy in row_ys:pts.extend(bezier((x,yy+65),(x+((i%2)*10-5),yy+82),(x+((i%2)*10-5),yy+106),(x,yy+124),8))
        gradient_path(im,pts,1,[(46,193,225),(64,223,192),(190,145,255)])
    for ri,(label,strap,names,notes,accents) in enumerate(groups):
        y=row_ys[ri]
        d=ImageDraw.Draw(im,"RGBA")
        rounded(d,(mx+13,y-2,mx+mw-13,y+122),12,(10,30,47,220),(40,67,86,145),1)
        text(d,(mx+27,y+7),label,8,COL["gold2"] if ri==0 else COL["text"],"bold")
        text(d,(mx+mw-24,y+9),strap,7,"#7f9cb1","sans","ra")
        start=mx+26;gap=8;cw=186;card_y=y+34
        for ci,(name,note,accent) in enumerate(zip(names,notes,accents)):
            x=start+ci*(cw+gap)
            rounded(d,(x,card_y,x+cw,card_y+73),9,(16,45,65,242),(72,119,146,130),1)
            d.rectangle((x,card_y+1,x+3,card_y+72),fill=accent)
            text(d,(x+11,card_y+9),name,8,"#e9f4fa","bold")
            wrapped(d,(x+11,card_y+31),note,cw-20,7,"#819caf","sans",9,2)
            d.ellipse((x+cw-13,card_y+9,x+cw-7,card_y+15),fill=accent)
    d=ImageDraw.Draw(im,"RGBA")
    text(d,(mx+24,938),"SHARED THREADS",7,COL["gold"],"bold")
    for i,(label,color) in enumerate([("run / parent IDs",COL["blue"]),("expected ↔ actual",COL["gold2"]),("evidence links",COL["green"]),("mutation lineage",COL["violet"]),("outcome receipts",COL["teal"])]):
        pill(d,mx+144+i*200,927,label,color,(7,25,39,235),(70,106,125,110),7,9,21)
    draw_complex_inspector(im,"ARCHITECTURE · REFERENCE MODEL","The planes stay woven","Task services do the work. The evidence fabric records it. The assurance plane checks it against the goal before any output can claim completion.","—","24 service cards","Deviations are process-wide","Shared correlation keys bind each service event to its parent run, evidence artifacts, policy result, and downstream output.")
    im.convert("RGB").save(path,format="PNG",optimize=True)


def draw_mutation_detail(path):
    im=make_background().convert("RGBA")
    draw_complex_header(im,"Mutation U-03 · trace before it spreads","A changed integration schema is detected, diffed, traced to its source, mapped to consumers, and quarantined until an authorized disposition.","MUTATION WATCH · FOCUSED SUB-PROCESS")
    d=ImageDraw.Draw(im,"RGBA");mx,my,mw,mh=246,286,1286,560
    rounded(d,(mx,my,mx+mw,my+mh),16,(11,32,49,247),(38,69,91,180),1)
    d.line((mx,my+57,mx+mw,my+57),fill=(34,61,80,190),width=1)
    rounded(d,(mx+15,my+13,mx+48,my+46),9,(29,75,95,185),(126,142,93,90),1)
    for dy in (0,5,10):d.line([(mx+21,my+25+dy),(mx+26,my+25+dy-2),(mx+31,my+25+dy),(mx+36,my+25+dy+2)],fill=COL["gold2"],width=2)
    text(d,(mx+60,my+15),"MUTATION U-03 · ORIGIN TO CONTAINMENT",9,COL["text"],"bold")
    text(d,(mx+60,my+33),"5 checkpoints · unknown origin · downstream consumption blocked",8,"#829db2","sans")
    pill(d,mx+1000,my+17,"DRILL-DOWN","#d3b4ff",(34,26,48,220),(117,89,154,150),7,10,23)
    text(d,(mx+16,my+71),"VIEW:  Schema mutation · before / after · impact · disposition",8,"#89a5ba","medium")
    cx,cy,cw,ch=mx+9,my+94,mw-18,309
    d.rounded_rectangle((cx,cy,cx+cw,cy+ch),radius=12,fill=(5,20,34,155))
    centers=[cx+int(cw*f) for f in [.09,.29,.50,.71,.91]];base=cy+149
    for i in range(4):gradient_path(im,bezier((centers[i]+76,base),(centers[i]+110,base),(centers[i+1]-110,base),(centers[i+1]-76,base),35),4)
    branch=bezier((centers[1],base),(centers[1]+70,base),(centers[1]+52,cy+44),(centers[2],cy+44),32)+bezier((centers[2],cy+44),(centers[2]+78,cy+44),(centers[2]+50,base),(centers[2]+70,base),32)[1:]
    gradient_path(im,branch,3,[(190,145,255),(72,187,255),(246,170,123)])
    lower=bezier((centers[2],base),(centers[2]+65,base),(centers[2]+49,cy+264),(centers[3],cy+264),35)+bezier((centers[3],cy+264),(centers[3]+70,cy+264),(centers[3]+54,base),(centers[3]+75,base),35)[1:]
    gradient_path(im,lower,3,[(255,188,142),(188,143,255),(65,193,230)])
    pill(d,centers[1]+84,cy+28,"UNKNOWN ORIGIN",COL["red"],(8,24,36,240),(147,77,67,130),7,9,20)
    pill(d,centers[3]-60,cy+249,"QUARANTINED · NO CONSUMERS",COL["gold2"],(8,24,36,240),(143,113,59,150),6,8,19)
    nodes=[("05.3.1","Detect difference","Baseline v4 → observed v4+1","Succeeded",100,"4 / 4","done"),("05.3.2","Snapshot states","Before / after digests retained","Succeeded",100,"3 / 3","done"),("05.3.3","Trace source","Writer and authorization unknown","Review",45,"2 / 4","attention"),("05.3.4","Map impact","Evidence and decision lanes held","In progress",58,"3 / 5","active"),("05.3.5","Contain change","Quarantine Q-09 remains active","Quarantined",100,"3 / 3","guarded")]
    for i,node in enumerate(nodes):
        x=centers[i];w=184;h=116;box=(x-w//2,base-h//2,x+w//2,base+h//2)
        draw_flow_card(im,box,*node[:6],selected=i==2,state=node[6])
    text(d,(cx+16,cy+ch-19),"●  Change detected",7,COL["green"],"medium");text(d,(cx+190,cy+ch-19),"●  Origin unresolved",7,COL["red"],"medium");text(d,(cx+386,cy+ch-19),"●  Impact mapping",7,COL["gold2"],"medium");text(d,(cx+572,cy+ch-19),"●  Consumers blocked",7,COL["waiting"],"medium")
    rounded(d,(246,705,1532,834),12,(12,34,51,235),(37,67,86,170),1)
    text(d,(264,723),"DOWNSTREAM IMPACT · CONTAINMENT PROPAGATES THROUGH THE DEPENDENCY GRAPH",8,COL["gold"],"bold")
    pathline=[("INTEGRATION I-09","schema +1 field","QUARANTINED",COL["violet"]),("EVIDENCE PACKAGE","two provenance links","HELD",COL["red"]),("POLICY DECISION","required evidence unknown","BLOCKED",COL["gold2"]),("API / UI RESULT","no success state","WITHHELD",COL["blue"])]
    for i,(head,desc,status,col) in enumerate(pathline):
        x=264+i*305
        rounded(d,(x,753,x+278,813),9,(14,39,58,235),(58,87,105,135),1)
        text(d,(x+11,761),head,7,col,"bold");text(d,(x+11,779),desc,7,"#c1d0dc","sans");pill(d,x+181,786,status,col,(8,24,36,240),(83,103,111,120),6,7,17)
        if i<3:
            d.line((x+279,783,x+300,783),fill=(237,195,110,170),width=2)
            d.polygon([(x+300,779),(x+306,783),(x+300,787)],fill=(237,195,110,210))
    rounded(d,(246,846,1532,978),12,(52,34,34,145),(132,75,67,150),1)
    text(d,(264,864),"EXPECTED VS ACTUAL",8,"#ffb09a","bold")
    text(d,(264,889),"Expected",8,"#8da6b8","sans");wrapped(d,(354,888),"Schema v4 · source authorized · all required consumers receive validated data.",520,8,"#d3e0e8","sans",11,2)
    text(d,(264,925),"Observed",8,"#8da6b8","sans");wrapped(d,(354,924),"One unknown field · origin not identified · response held before evidence or policy can consume it.",520,8,"#ffe0c6","sans",11,2)
    text(d,(964,889),"Mutation scope",8,"#8da6b8","sans");wrapped(d,(1060,888),"Integration → evidence package → policy → API / UI result",430,8,"#d3e0e8","sans",11,2)
    text(d,(964,925),"Disposition",8,"#8da6b8","sans");text(d,(1060,925),"QUARANTINED · REVIEW REQUIRED",8,COL["gold2"],"bold")
    draw_complex_inspector(im,"MUTATION U-03 · REVIEW","Unrecognized schema field","The system records the changed shape and both digests. Its origin is still unknown, so dependent paths remain held.","54%","2 of 4","1 active mutation","Before: integration schema v4. Actual: one additional field. No downstream process consumes it until a reviewer records an authorized disposition.")
    im.convert("RGB").save(path,format="PNG",optimize=True)


if __name__ == "__main__":
    OUT.mkdir(parents=True,exist_ok=True)
    draw_screen("overview",OUT/"overview.png")
    draw_screen("face-check",OUT/"verification-detail.png")
    draw_screen("button-build",OUT/"button-lineage.png")
    draw_complex_overview(OUT/"complex-overview.png")
    draw_architecture(OUT/"architecture.png")
    draw_mutation_detail(OUT/"mutation-detail.png")
    print("Created: simple views and complex overview, architecture, mutation detail")
