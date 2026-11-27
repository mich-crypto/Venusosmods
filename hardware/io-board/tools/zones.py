import pcbnew
mm=pcbnew.FromMM; OX,OY=100,100
b=pcbnew.LoadBoard('io-board.kicad_pcb')
gnd=b.FindNet('GND')
for layer in (pcbnew.F_Cu,pcbnew.B_Cu):
    z=pcbnew.ZONE(b); z.SetLayer(layer); z.SetNet(gnd)
    o=z.Outline(); o.NewOutline()
    for x,y in [(0.5,0.5),(119.5,0.5),(119.5,139.5),(0.5,139.5)]: o.Append(mm(OX+x),mm(OY+y))
    z.SetLocalClearance(mm(0.3)); z.SetMinThickness(mm(0.25))
    z.SetPadConnection(pcbnew.ZONE_CONNECTION_THERMAL); z.SetThermalReliefGap(mm(0.4)); z.SetThermalReliefSpokeWidth(mm(0.5))
    z.SetAssignedPriority(0); b.Add(z)
pcbnew.ZONE_FILLER(b).Fill(b.Zones())
pcbnew.SaveBoard('io-board.kicad_pcb',b)
pcbnew.ExportSpecctraDSN(b,'io-board.dsn')
print('zones',len(list(b.Zones())))
