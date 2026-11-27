# net classes into the KiCad project: Default 0.25 mm (0.15 mm clearance), Power 0.3 mm (0.15 mm clearance, reaches 0.5 mm pitch pins), HighCurrent 2.0 mm (5 A, 0.3 mm clearance)
import json
d=json.load(open('io-board.kicad_pro'))
base=[c for c in d['net_settings']['classes'] if c['name']=='Default'][0]; base['track_width']=0.25; base['clearance']=0.15
def cls(name,w,cl): c=dict(base); c.update(name=name,track_width=w,clearance=cl); return c
d['net_settings']['classes']=[base,cls('Power',0.3,0.15),cls('HighCurrent',2.0,0.3)]
d['net_settings']['netclass_patterns']=[{'netclass':'HighCurrent','pattern':p} for p in ['V12_AUX*','SH?_?','OUT?_C','OUT?_NO','OUT?_NC','PD_SUPPLY','PD_12V']]+\
  [{'netclass':'Power','pattern':p} for p in ['V12_SYS','ST1_12V*','+5V','+3V3','LIN_VBAT','FRESH_5V','TRUMA_12V','EXP_12V','GND','VBUS_PROG','K?_COIL']]
d['board']['design_settings']['rules']['min_resolved_spokes']=1
d['board']['design_settings']['rules']['min_hole_clearance']=0.19  # GCT USB4105 footprint: NPTH pegs 0.194 mm from its GND pads
json.dump(d,open('io-board.kicad_pro','w'),indent=2)
