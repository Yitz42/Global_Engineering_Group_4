"""Figure 1 for the report: the MeshLearn system diagram.

Usage: python3 make_system_figure.py docs/figures
Renders the SVG below with headless Chrome at 2x for print. No extra packages.
"""
import os
import re
import subprocess
import sys
import tempfile

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
OUT = sys.argv[1]
WORK = tempfile.mkdtemp(prefix="meshlearn-figure-")
FLAGS = ["--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run", "--no-default-browser-check",
         "--force-color-profile=srgb", "--virtual-time-budget=3000", "--allow-file-access-from-files"]
MEASURE = "<script>addEventListener('load',()=>{document.title='H'+Math.ceil(document.documentElement.getBoundingClientRect().height)})</script>"
FONT = "-apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"


def chrome(args, timeout=90):
    return subprocess.run([CHROME, *FLAGS, *args], capture_output=True, text=True, timeout=timeout)




def compose(html, width, name):
    """Render an HTML composition to OUT/name.png, sized to its content."""
    path = os.path.join(WORK, name + ".html")
    with open(path, "w") as fh:
        fh.write("<!doctype html><html><head><meta charset='utf-8'>" + MEASURE +
                 f"<style>html,body{{margin:0;background:#fff}}body{{font-family:{FONT};color:#16202e}}</style>"
                 "</head><body>" + html + "</body></html>")
    dom = chrome([f"--window-size={width},600", "--dump-dom", "file://" + path]).stdout
    m = re.search(r"<title>H(\d+)</title>", dom)
    height = int(m.group(1)) if m else 900
    png = os.path.join(OUT, name + ".png")
    chrome([f"--window-size={width},{height}", "--force-device-scale-factor=2", f"--screenshot={png}", "file://" + path])
    print(f"{name}: {width}x{height} css px, {os.path.getsize(png) // 1024} KB")






# -- Figure: system overview
SYSTEM = """
<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 1600 820' width='1600' height='820' font-family="-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif">
 <defs>
  <marker id='ar' viewBox='0 0 10 10' refX='9' refY='5' markerWidth='7' markerHeight='7' orient='auto-start-reverse'><path d='M0 0L10 5L0 10z' fill='#1f5fbf'/></marker>
  <marker id='ag' viewBox='0 0 10 10' refX='9' refY='5' markerWidth='7' markerHeight='7' orient='auto-start-reverse'><path d='M0 0L10 5L0 10z' fill='#7a869a'/></marker>
 </defs>
 <rect width='1600' height='820' fill='#fff'/>
 <text x='40' y='50' font-size='30' font-weight='700' fill='#16202e'>The radio carries answers and grades; lessons travel ahead on the memory card</text>

 <!-- school -->
 <rect x='40' y='90' width='450' height='470' rx='16' fill='#f3f6fb' stroke='#c9d4e6'/>
 <text x='64' y='130' font-size='22' font-weight='700' fill='#56637a' letter-spacing='1'>SCHOOL</text>
 <rect x='70' y='155' width='390' height='190' rx='12' fill='#fff' stroke='#9fb0c9' stroke-width='1.5'/>
 <text x='94' y='195' font-size='26' font-weight='700' fill='#16202e'>Teacher hub (laptop)</text>
 <text x='94' y='230' font-size='20' fill='#3a4658'>Courses, assignments, grading</text>
 <text x='94' y='260' font-size='20' fill='#3a4658'>Comprehension by student and unit</text>
 <text x='94' y='290' font-size='20' fill='#3a4658'>Card library, learning-data export</text>
 <text x='94' y='320' font-size='20' fill='#3a4658'>Python, no internet needed</text>
 <rect x='70' y='395' width='390' height='120' rx='12' fill='#fff' stroke='#9fb0c9' stroke-width='1.5'/>
 <text x='94' y='437' font-size='24' font-weight='700' fill='#16202e'>LoRa radio (USB)</text>
 <text x='94' y='470' font-size='20' fill='#3a4658'>Meshtastic node, e.g. RAK4631</text>
 <text x='94' y='498' font-size='20' fill='#3a4658'>915 MHz (US, Brazil) · 868 MHz (EU)</text>
 <line x1='265' y1='345' x2='265' y2='393' stroke='#7a869a' stroke-width='2.5'/>
 <text x='278' y='375' font-size='18' fill='#56637a'>USB cable</text>

 <!-- village -->
 <rect x='1110' y='90' width='450' height='690' rx='16' fill='#f6f8f3' stroke='#cbd8c4'/>
 <text x='1134' y='130' font-size='22' font-weight='700' fill='#56637a' letter-spacing='1'>VILLAGE OR HOUSEHOLD</text>
 <rect x='1140' y='155' width='390' height='120' rx='12' fill='#fff' stroke='#a7bb9c' stroke-width='1.5'/>
 <text x='1164' y='197' font-size='24' font-weight='700' fill='#16202e'>LoRa radio (Bluetooth)</text>
 <text x='1164' y='230' font-size='20' fill='#3a4658'>e.g. Wio Tracker L1, paired once</text>
 <text x='1164' y='258' font-size='20' fill='#3a4658'>Solar or USB power</text>
 <rect x='1140' y='325' width='390' height='200' rx='12' fill='#fff' stroke='#a7bb9c' stroke-width='1.5'/>
 <text x='1164' y='367' font-size='26' font-weight='700' fill='#16202e'>Old Android phone</text>
 <text x='1164' y='402' font-size='20' fill='#3a4658'>microSD: assignments + library</text>
 <text x='1164' y='432' font-size='20' fill='#3a4658'>Student website on its hotspot</text>
 <text x='1164' y='462' font-size='20' fill='#3a4658'>Checks answers, batches work</text>
 <text x='1164' y='492' font-size='20' fill='#3a4658'>Termux, Python</text>
 <line x1='1335' y1='275' x2='1335' y2='323' stroke='#1f5fbf' stroke-width='2.5' stroke-dasharray='6 5'/>
 <text x='1348' y='305' font-size='18' font-weight='600' fill='#1f5fbf'>Bluetooth</text>
 <!-- student devices -->
 <g font-size='19' fill='#16202e' text-anchor='middle'>
  <rect x='1140' y='600' width='118' height='150' rx='12' fill='#fff' stroke='#a7bb9c' stroke-width='1.5'/><text x='1199' y='668'>Student</text><text x='1199' y='693'>phone</text>
  <rect x='1276' y='600' width='118' height='150' rx='12' fill='#fff' stroke='#a7bb9c' stroke-width='1.5'/><text x='1335' y='668'>Tablet</text>
  <rect x='1412' y='600' width='118' height='150' rx='12' fill='#fff' stroke='#a7bb9c' stroke-width='1.5'/><text x='1471' y='668'>Shared</text><text x='1471' y='693'>laptop</text>
 </g>
 <path d='M1335 525 V560 M1199 560 H1471 M1199 560 V598 M1335 560 V598 M1471 560 V598' stroke='#7a869a' stroke-width='2.5' fill='none'/>
 <text x='1348' y='552' font-size='18' fill='#56637a'>Wi-Fi hotspot</text>

 <!-- mesh -->
 <circle cx='800' cy='300' r='58' fill='#fff' stroke='#b4690e' stroke-width='2.5'/>
 <text x='800' y='294' font-size='20' font-weight='700' text-anchor='middle' fill='#16202e'>Solar</text>
 <text x='800' y='318' font-size='20' font-weight='700' text-anchor='middle' fill='#16202e'>relay</text>
 <text x='800' y='392' font-size='18' text-anchor='middle' fill='#56637a'>optional, extends range</text>
 <path d='M462 420 C 560 380, 640 320, 742 300' stroke='#b4690e' stroke-width='3' stroke-dasharray='10 8' fill='none'/>
 <path d='M858 300 C 960 290, 1040 230, 1138 215' stroke='#b4690e' stroke-width='3' stroke-dasharray='10 8' fill='none'/>
 <text x='800' y='196' font-size='19' font-weight='700' text-anchor='middle' fill='#b4690e'>LoRa mesh, several km per hop</text>

 <!-- radio messages -->
 <g font-size='19' fill='#16202e'>
  <rect x='560' y='450' width='480' height='132' rx='12' fill='#fffaf2' stroke='#e7c48f'/>
  <text x='584' y='484' font-weight='700'>Over the radio (219-byte packets)</text>
  <text x='584' y='514'>→ Open an assignment: 1 packet</text>
  <text x='584' y='542'>← A batch of students' answers: 1–2 packets</text>
  <text x='584' y='570'>→ A batch of grades: 1 packet</text>
 </g>

 <!-- offline loading -->
 <path d='M265 560 V700 H1100' stroke='#7a869a' stroke-width='2.5' stroke-dasharray='4 6' fill='none' marker-end='url(#ag)'/>
 <text x='300' y='690' font-size='20' fill='#3a4658'>At school, no radio: every assignment and the offline library are copied to the</text>
 <text x='300' y='718' font-size='20' fill='#3a4658'>phone over school Wi-Fi or on the microSD card (Wikipedia, textbooks, PhET, books)</text>
</svg>"""
compose(SYSTEM, 1600, "fig-system-overview")
