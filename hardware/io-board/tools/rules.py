# net classes into the KiCad project: Default 0.25 mm, Power 0.6 mm, HighCurrent 2.0 mm (5 A, 0.4 mm clearance)
import json
d=json.load(open('io-board.kicad_pro'))
base=[c for c in d['net_settings']['classes'] if c['name']=='Default'][0]; base['track_width']=0.25
def cls(name,w,cl): c=dict(base); c.update(name=name,track_width=w,clearance=cl); return c
d['net_settings']['classes']=[base,cls('Power',0.6,0.25),cls('HighCurrent',2.0,0.4)]
d['net_settings']['netclass_patterns']=[{'netclass':'HighCurrent','pattern':p} for p in ['V12_AUX*','SH?_?','OUT?_C','OUT?_NO','OUT?_NC','PD_SUPPLY','PD_12V']]+\
  [{'netclass':'Power','pattern':p} for p in ['V12_SYS','ST1_12V*','+5V','+3V3','LIN_VBAT','FRESH_5V','TRUMA_12V','EXP_12V','GND','VBUS_PROG','K?_COIL']]
json.dump(d,open('io-board.kicad_pro','w'),indent=2)
