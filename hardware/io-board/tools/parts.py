# Netlist of the I/O board: (ref, lib:symbol, value, footprint, {pin: net}, note)
R6='Resistor_SMD:R_0603_1608Metric'; C6='Capacitor_SMD:C_0603_1608Metric'; C8='Capacitor_SMD:C_0805_2012Metric'
P=[]
def add(ref,sym,val,fp,conn,note=''): P.append((ref,sym,val,fp,conn,note))
def R(ref,val,a,b,fp=R6): add(ref,'Device:R',val,fp,{'1':a,'2':b})
def C(ref,val,a,b,fp=C6): add(ref,'Device:C',val,fp,{'1':a,'2':b})
def D(ref,sym,val,fp,a,k): add(ref,sym,val,fp,{'2':a,'1':k})
G='GND'
# ---------------- 1 power
add('J4','Connector_Generic:Conn_02x03_Odd_Even','ST1 EBL','Connector_Molex:Molex_Micro-Fit_3.0_43045-0600_2x03_P3.00mm_Horizontal',
    {'1':'EBL_OFF','2':'EBL_ON','3':'EBL_12V','4':'EBL_MAINS','5':G,'6':'ST1_12V'},'1 yellow OFF, 2 green ON, 3 white 12V-on, 4 pink mains, 5 brown GND, 6 blue +12V')
add('F1','Device:Polyfuse','0.5A 30V PTC','Fuse:Fuse_1206_3216Metric',{'1':'ST1_12V','2':'ST1_12V_F'})
D('D1','Diode:SS34','SS34','Diode_SMD:D_SMA','ST1_12V_F','V12_SYS')
add('J5','Connector:Screw_Terminal_01x04','KF2EDGR-5.0-4P','Connector_Phoenix_MSTB:PhoenixContact_MSTBA_2,5_4-G_1x04_P5.00mm_Horizontal',
    {'1':'V12_AUX_IN','2':G,'3':'TRUMA_12V','4':G},'pluggable 5.0 mm (plug KF2EDGK-5.0-4P): 1 12V AUX+ (EBL Spare 4), 2 GND, 3 CP plus 12V+, 4 GND')
for n,ref in ((1,'J11'),(2,'J12')):
    add(ref,'Connector:Screw_Terminal_01x03','KF2EDGR-5.0-3P','Connector_Phoenix_MSTB:PhoenixContact_MSTBA_2,5_3-G_1x03_P5.00mm_Horizontal',
        {'1':f'OUT{n}_C','2':f'OUT{n}_NO','3':f'OUT{n}_NC'},f'pluggable 5.0 mm (plug KF2EDGK-5.0-3P): Output {n} C / NO / NC')
add('F2','Device:Fuse','10A 0451010.MRL','Fuse:Fuse_Littelfuse-NANO2-451_453',{'1':'V12_AUX_IN','2':'V12_AUX_F'})
# reverse-polarity protection of 12 V AUX: P-MOSFET, drain to the fused input, source to the load side
add('Q3','Device:Q_PMOS_GDS','AOD4185','Package_TO_SOT_SMD:TO-252-2',{'1':'Q3_G','2':'V12_AUX_F','3':'V12_AUX'},'AOS -40 V, 15 mOhm at -10 V: 0.4 W at 5 A, 1.5 W at 10 A; reversed AUX keeps it off')
R('R7','10k','Q3_G',G)
D('D13','Device:D_Zener','BZT52C12','Diode_SMD:D_SOD-123','Q3_G','V12_AUX')  # VGS clamp -12 V (max +-20 V)
add('D14','Device:D_TVS','SMBJ15A','Diode_SMD:D_SMB',{'1':'V12_AUX','2':G},'unidirectional, band to V12_AUX; clamps 24.4 V, below INA180 26 V and the 30 V MOSFETs')
D('D2','Diode:SS34','SS34','Diode_SMD:D_SMA','V12_AUX','V12_SYS')
add('D4','Device:D_TVS','SMBJ18A','Diode_SMD:D_SMB',{'1':'V12_SYS','2':G},'unidirectional, band to V12_SYS; clamps 29.2 V, below C1 35 V and TSR-1 36 V')
C('C1','10u/50V','V12_SYS',G,'Capacitor_SMD:C_1210_3225Metric')
add('U1','Regulator_Switching:TSR_1-2450','TSR 1-2450','Converter_DCDC:Converter_DCDC_TRACO_TSR-1_THT',{'1':'V12_SYS','2':G,'3':'+5V'})
C('C2','22u','+5V',G,C8)  # at U2 input
C('C9','10u','+5V',G,C8)  # at U1 (TSR-1) output
add('U2','Regulator_Linear:AP7361C-33E','AP7361C-33E','Package_TO_SOT_SMD:SOT-223-3_TabPin2',{'1':'+5V','2':G,'3':'+3V3'},'-33E = SOT223: 1 IN, 2 GND (tab), 3 OUT')
C('C3','22u','+3V3',G,C8)
D('D3','Diode:SS34','SS14','Diode_SMD:D_SMA','VBUS_PROG','+5V')
R('R1','100k','ST1_12V','VBAT_SENSE'); R('R2','22k','VBAT_SENSE',G); C('C4','100n','VBAT_SENSE',G)
# ---------------- 2 ESP32
esp={'1':G,'40':G,'41':G,'2':'+3V3','3':'EN'}
io={'IO4':'FRESH_PRESSURE','IO5':'FRESH_TEMP','IO9':'GREY_COM_DRV','IO1':'GREY_ROD1','IO2':'GREY_ROD2','IO7':'GREY_ROD3','IO8':'GREY_ROD4',
    'IO10':'VBAT_SENSE','IO11':'IN1','IO12':'IN2','IO13':'IN3','IO14':'OUT1_SENSE','IO6':'OUT2_SENSE','IO15':'SDA','IO16':'SCL','IO47':'TILT_INT','IO40':'EXP_INT_MCU',
    'IO17':'LIN_RXD','IO18':'LIN_TXD','IO21':'LIN_SLP','IO38':'OUT1_DRV','IO39':'OUT2_DRV','IO35':'EBL_ON_DRV','IO36':'EBL_OFF_DRV',
    'IO37':'EBL_12V_SENSE','IO42':'EBL_MAINS_SENSE','IO41':'USB_PD_EN','IO48':'LED_DATA','IO19':'USB_DM','IO20':'USB_DP','IO0':'BOOT'}
add('U3','RF_Module:ESP32-S3-WROOM-1','ESP32-S3-WROOM-1-N8','RF_Module:ESP32-S3-WROOM-1',dict(esp,**{'@'+k:v for k,v in io.items()}),'N8 without PSRAM: the -N8R8/-N16R8 use IO35-IO37 for octal PSRAM')
C('C5','10u','+3V3',G,C8); C('C6','100n','+3V3',G)
R('R3','10k','+3V3','EN'); C('C7','1u','EN',G)
add('SW1','Switch:SW_Push','TS-1187A-B-A-B','Button_Switch_SMD:SW_Push_1P1T_XKB_TS-1187A',{'1':'EN','2':G})
add('SW2','Switch:SW_Push','TS-1187A-B-A-B','Button_Switch_SMD:SW_Push_1P1T_XKB_TS-1187A',{'1':'BOOT','2':G})
add('J1','Connector:USB_C_Receptacle_USB2.0_16P','USB PROG','Connector_USB:USB_C_Receptacle_GCT_USB4105-xx-A_16P_TopMnt_Horizontal',
    {'A1':G,'A12':G,'B1':G,'B12':G,'S1':G,'A4':'VBUS_PROG','A9':'VBUS_PROG','B4':'VBUS_PROG','B9':'VBUS_PROG','A5':'CC1_PROG','B5':'CC2_PROG',
     'A6':'USB_DP','B6':'USB_DP','A7':'USB_DM','B7':'USB_DM','A8':'NC','B8':'NC'})
R('R4','5.1k','CC1_PROG',G); R('R5','5.1k','CC2_PROG',G)
add('U4','Power_Protection:USBLC6-2SC6','USBLC6-2SC6','Package_TO_SOT_SMD:SOT-23-6',{'1':'USB_DM','6':'USB_DM','3':'USB_DP','4':'USB_DP','5':'VBUS_PROG','2':G})
add('D5','LED:SK6812MINI','SK6812MINI-HS','io_board:LED_SK6812MINI-HS_3.7x3.5mm',{'4':'LED_VDD','2':G,'3':'LED_DIN','1':'NC'},'status LED, front plate; SK6812MINI (3.5 x 3.5, top mount), not the -E (3.2 x 2.8, other pinout)')
D('D6','Diode:1N4148W','1N4148W','Diode_SMD:D_SOD-123','+5V','LED_VDD')  # LED at 4.3 V so 3.3 V data meets VIH 0.7 VDD
R('R6','330','LED_DATA','LED_DIN'); C('C8','100n','LED_VDD',G)
# ---------------- 3 EBL
for ref,rr,drv,line in [('U5','R10','EBL_ON_DRV','EBL_ON'),('U6','R11','EBL_OFF_DRV','EBL_OFF')]:
    R(rr,'330',drv,ref+'_LED')
    add(ref,'Relay_SolidState:TLP3123','AQY212EH','Package_DIP:SMDIP-4_W9.53mm',{'1':ref+'_LED','2':G,'3':G,'4':line},'photoMOS, pulses the EBL line to GND (AQY212EH or TLP3123, SO-4)')
R('R12','4.7k','EBL_12V','N_12V_Z'); D('D7','Device:D_Zener','4V7','Diode_SMD:D_SOD-123','N_12V_A','N_12V_Z')
add('U7','Isolator:PC817','PC817','Package_DIP:SMDIP-4_W9.53mm',{'1':'N_12V_A','2':G,'4':'EBL_12V_SENSE','3':G})
R('R15','10k','+3V3','EBL_12V_SENSE')
R('R13','2.2k','EBL_MAINS','N_MAINS_Z'); D('D8','Device:D_Zener','2V4','Diode_SMD:D_SOD-123','N_MAINS_A','N_MAINS_Z')
add('U8','Isolator:PC817','PC817','Package_DIP:SMDIP-4_W9.53mm',{'1':'N_MAINS_A','2':G,'4':'EBL_MAINS_SENSE','3':G})
R('R16','10k','+3V3','EBL_MAINS_SENSE')
R('R14','2.2k','EBL_12V','N_LED12'); D('D9','Device:LED','green 0603','LED_SMD:LED_0603_1608Metric','N_LED12',G)  # at the front edge, light pipe
add('SW3','Switch:SW_Push','SKHHLQA010','Button_Switch_THT:SW_Tactile_SKHH_Angled',{'1':'EBL_ON','2':G},'front plate, works without the ESP32')
add('SW4','Switch:SW_Push','SKHHLQA010','Button_Switch_THT:SW_Tactile_SKHH_Angled',{'1':'EBL_OFF','2':G},'front plate, works without the ESP32')
# ---------------- 4 tanks
add('J6','Connector_Generic:Conn_01x06','ST2 FRESH','Connector_Molex:Molex_Micro-Fit_3.0_43650-0600_1x06_P3.00mm_Horizontal',
    {'1':'FRESH_T_RAW','2':'FRESH_P_RAW','3':G,'4':'FRESH_5V','5':'FRESH_3V3','6':'NC'},'1 grey temp data, 2 yellow pressure, 3 green GND, 4 brown +5V, 5 white +3.3V, 6 spare')
R('R20','100','+3V3','FRESH_3V3'); add('F3','Device:Polyfuse','50mA 15V PTC','Fuse:Fuse_1206_3216Metric',{'1':'+5V','2':'FRESH_5V'})
add('D20','Device:D_TVS','ESD 5V','Diode_SMD:D_SOD-323',{'1':'FRESH_P_RAW','2':G})
R('R21','10k','FRESH_P_RAW','FRESH_PRESSURE'); C('C20','100n','FRESH_PRESSURE',G); R('R22','1M','FRESH_PRESSURE',G)
add('D21','Device:D_TVS','ESD 5V','Diode_SMD:D_SOD-323',{'1':'FRESH_T_RAW','2':G})
R('R23','100','FRESH_T_RAW','FRESH_TEMP'); R('R24','4.7k','+3V3','FRESH_TEMP')
add('J7','Connector_Generic:Conn_01x05','ST3 GREY','Connector_Molex:Molex_Micro-Fit_3.0_43650-0500_1x05_P3.00mm_Horizontal',
    {'1':'ROD4_RAW','2':'ROD3_RAW','3':'ROD2_RAW','4':'ROD1_RAW','5':'GREY_COM'},'1 full, 2 3/4, 3 1/2, 4 1/4, 5 base (common)')
R('R30','1k','GREY_COM_DRV','GREY_COM')
for k in range(1,5):
    R(f'R3{k}','10k',f'ROD{k}_RAW',f'GREY_ROD{k}'); C(f'C3{k}','100n',f'GREY_ROD{k}',G); R(f'R4{k}','100k',f'ROD{k}_RAW',G)
    add(f'D3{k}','Device:D_TVS','ESD 5V','Diode_SMD:D_SOD-323',{'1':f'ROD{k}_RAW','2':G})
# ---------------- 5 outputs
R('R59','100k','+3V3','VREF'); R('R58','4.7k','VREF',G)
for n in (1,2):
    add(f'F{3+n}','Device:Fuse','5A 0451005.MRL','Fuse:Fuse_Littelfuse-NANO2-451_453',{'1':'V12_AUX','2':f'SH{n}_P'})
    R(f'R5{n-1}','0.05 2W','SH'+str(n)+'_P',f'SH{n}_N','Resistor_SMD:R_2512_6332Metric')  # 5 A: 1.25 W
    add(f'JP{n}','Jumper:SolderJumper_2_Bridged','1m 2W 2512 (C-feed link)','Resistor_SMD:R_2512_6332Metric',{'1':f'SH{n}_N','2':f'OUT{n}_C'},'5 A path: 0 ohm 2512 jumper rated >= 6 A, or a wire link; remove for external C feed')
    add(f'K{n}','Relay:SANYOU_SRD_Form_C','SRD-12VDC-SL-C','Relay_THT:Relay_SPDT_SANYOU_SRD_Series_Form_C',{'5':'V12_AUX','2':f'K{n}_COIL','1':f'OUT{n}_C','3':f'OUT{n}_NO','4':f'OUT{n}_NC'},f'Output {n}')
    add(f'Q{n}','Transistor_FET:AO3400A','AO3400A','Package_TO_SOT_SMD:SOT-23',{'1':f'Q{n}_G','2':G,'3':f'K{n}_COIL'})
    R(f'R{n}05','1k',f'OUT{n}_DRV',f'Q{n}_G'); R(f'R{n}06','100k',f'Q{n}_G',G)
    D(f'D1{n}','Diode:1N4148W','1N4148W','Diode_SMD:D_SOD-123',f'K{n}_COIL','V12_AUX')
    R(f'R{n}07','2.2k','V12_AUX',f'K{n}_LED'); D(f'D2{n+1}' if False else f'D4{n}','Device:LED','red','LED_SMD:LED_0603_1608Metric',f'K{n}_LED',f'K{n}_COIL')
    add(f'U{n}1','Amplifier_Current:INA180A3','INA180A3','Package_TO_SOT_SMD:SOT-23-5',{'3':f'SH{n}_SP','4':f'SH{n}_SN','1':f'ISENSE{n}','2':G,'5':'+3V3'})
    add(f'NT{n}1','Device:NetTie_2','NetTie','NetTie:NetTie-2_SMD_Pad0.5mm',{'1':f'SH{n}_P','2':f'SH{n}_SP'},'Kelvin tap at the shunt')
    add(f'NT{n}2','Device:NetTie_2','NetTie','NetTie:NetTie-2_SMD_Pad0.5mm',{'1':f'SH{n}_N','2':f'SH{n}_SN'},'Kelvin tap at the shunt')
    add(f'U{n}2','Comparator:LMV331','LMV331','Package_TO_SOT_SMD:SOT-23-5',{'1':f'ISENSE{n}','3':'VREF','4':f'OUT{n}_SENSE','2':G,'5':'+5V'},'5 V supply: input range to 4.2 V covers the INA180 output (up to 3.3 V); open-drain output pulled to 3.3 V')
    C(f'C9{2+2*n}','100n','+3V3',G)  # INA180 VS bypass
    C(f'C9{3+2*n}','100n','+5V',G)   # LMV331 VCC bypass
    R(f'R{n}08','10k','+3V3',f'OUT{n}_SENSE')
# ---------------- 6 Truma + inputs
for ref,v in [('J8','RJ12 Truma IN'),('J9','RJ12 Truma OUT')]:
    add(ref,'Connector:RJ12','DS1133-S60BPX','io_board:RJ12_Connfly_DS1133-S60_Horizontal',{'1':'TR1','2':'TR2','3':'LIN','4':'TR4','5':G,'6':'TR6'},'pin 3 LIN, 5 GND (inetbox.py), others 1:1')
add('D60','Device:D_TVS','PESD1LIN','Diode_SMD:D_SOD-323',{'1':'LIN','2':G})
D('D61','Diode:SS34','SS14','Diode_SMD:D_SMA','V12_SYS','LIN_VBAT')
C('C60','100n','LIN_VBAT',G)
add('U60','Interface_CAN_LIN:TJA1021T','TJA1021T/20','Package_SO:SOIC-8_3.9x4.9mm_P1.27mm',{'1':'LIN_RXD','2':'LIN_SLP','3':'LIN_WAKE','4':'LIN_TXD','5':G,'6':'LIN','7':'LIN_VBAT','8':'NC'})
R('R61','10k','LIN_VBAT','LIN_WAKE'); R('R62','10k','+3V3','LIN_RXD'); R('R63','100k','LIN_SLP',G)
add('F6','Device:Polyfuse','1A 30V PTC','Fuse:Fuse_1812_4532Metric',{'1':'V12_AUX','2':'TRUMA_12V'},'12 V for the CP plus panel')
add('J10','Connector:Screw_Terminal_01x04','KF2EDGR-3.5-4P','Connector_Phoenix_MC:PhoenixContact_MC_1,5_4-G-3.5_1x04_P3.50mm_Horizontal',{'1':'IN_COM','2':'IN1_T','3':'IN2_T','4':'IN3_T'},'1 COM, 2 IN1 (3Gas+), 3 IN2, 4 IN3')
add('JP10','Jumper:SolderJumper_2_Bridged','JP COM','Jumper:SolderJumper-2_P1.3mm_Bridged_RoundedPad1.0x1.5mm',{'1':'IN_COM','2':G})
for k in (1,2,3):
    D(f'D7{k}','Diode:1N4148W','1N4148W','Diode_SMD:D_SOD-123',f'IN{k}_T',f'IN{k}_D')
    R(f'R7{k}','4.7k 0.25W',f'IN{k}_D',f'IN{k}_A','Resistor_SMD:R_1206_3216Metric')  # 30 V: 169 mW
    add(f'U7{k}','Isolator:PC817','PC817','Package_DIP:SMDIP-4_W9.53mm',{'1':f'IN{k}_A','2':'IN_COM','4':f'IN{k}','3':G})
    R(f'R7{k+3}','10k','+3V3',f'IN{k}')
# ---------------- 7 USB PD module, sensors, expansion
add('F7','Device:Fuse','4A 0451004.MRL','Fuse:Fuse_Littelfuse-NANO2-451_453',{'1':'V12_AUX','2':'PD_SUPPLY'})
add('Q80','Transistor_FET:AO3401A','AO3401A','Package_TO_SOT_SMD:SOT-23',{'2':'PD_SUPPLY','3':'PD_12V','1':'PD_G'})
R('R80','100k','PD_SUPPLY','PD_G')
add('Q81','Transistor_FET:2N7002','2N7002','Package_TO_SOT_SMD:SOT-23',{'3':'PD_G_DRV','2':G,'1':'PD_EN_G'})
R('R83','100k','PD_G_DRV','PD_G')  # with R80: VGS = -V/2, inside AO3401A +-12 V
R('R81','1k','USB_PD_EN','PD_EN_G'); R('R82','100k','PD_EN_G',G)
add('J2','Connector_Generic:Conn_01x02','PD module','Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical',{'1':'PD_12V','2':G},'ready-made 12 V to USB-C PD 30 W module, its socket in the front plate')
R('R90','4.7k','+3V3','SDA'); R('R91','4.7k','+3V3','SCL'); R('R93','10k','+3V3','EXP_INT_MCU')
add('U90','Sensor_Motion:LIS3DH','LIS3DH','Package_LGA:LGA-16_3x3mm_P0.5mm_LayoutBorder3x5y',
    {'1':'+3V3','14':'+3V3','5':G,'10':G,'12':G,'4':'SCL','6':'SDA','7':G,'8':'+3V3','11':'TILT_INT','9':'NC','13':'NC','15':'NC','16':'NC','2':'NC','3':'NC'},'tilt, I2C 0x18 (LIS2DH12 is LGA-12 2x2, not this footprint)')
C('C90','100n','+3V3',G)
add('U91','Sensor_Humidity:SHT4x','SHT40-AD1B','Sensor_Humidity:Sensirion_DFN-4_1.5x1.5mm_P0.8mm_SHT4x_NoCentralPad',{'1':'SDA','2':'SCL','3':'+3V3','4':G},'humidity + temperature, I2C 0x44')
C('C91','100n','+3V3',G)
add('U92','io_board:PCA9615','PCA9615DP','Package_SO:TSSOP-10_3x3mm_P0.5mm',{'1':'+3V3','2':'SDA','3':'+3V3','4':'SCL','5':G,'6':'DSCL_M','7':'DSCL_P','8':'DSDA_P','9':'DSDA_M','10':'+5V'},'pinout from NXP PCA9615 Rev. 2, Table 3: 1 VDD(A), 2 SDA, 3 EN, 4 SCL, 5 VSS, 6 DSCLM, 7 DSCLP, 8 DSDAP, 9 DSDAM, 10 VDD(B)')
C('C92','100n','+3V3',G); C('C93','100n','+5V',G)
add('F8','Device:Polyfuse','1A 30V PTC','Fuse:Fuse_1812_4532Metric',{'1':'V12_AUX','2':'EXP_12V'})
R('R92','100','EXP_INT_MCU','EXP_INT')  # expansion interrupt: open drain on the module side, own GPIO
# dI2C termination + idle bias at this end of the cable (NXP PCA9615 Fig. 5/7: 600-120-600 at VDD(B) 5 V)
for (pp,mm,ra,rb,rc) in (('DSCL_P','DSCL_M','R94','R95','R96'),('DSDA_P','DSDA_M','R97','R98','R99')):
    R(ra,'620','+5V',pp); R(rb,'120',pp,mm); R(rc,'620',mm,G)
add('U93','Power_Protection:ESDA6V1-5SC6','ESDA6V1-5SC6','Package_TO_SOT_SMD:SOT-23-6',{'1':'DSDA_P','3':'DSDA_M','4':'DSCL_P','5':'DSCL_M','6':'EXP_INT','2':G},'ESD on the expansion cable lines')
add('J3','Connector:RJ45_Shielded','DS1128-09-S8B8S','Connector_RJ:RJ45_Connfly_DS1128-09-S8xx-S_Horizontal',{'SH':G,'1':'EXP_12V','2':'EXP_12V','3':'DSDA_P','6':'DSDA_M','4':'DSCL_P','5':'DSCL_M','7':'EXP_INT','8':G})
# power flags
FLAGS=['GND','V12_SYS','V12_AUX','ST1_12V','LIN_VBAT','PD_12V','+5V','+3V3','VBUS_PROG']

# parts placed on the board but not fitted in this build (assembly 'do not place')
DNP={'U92'}   # PCA9615: not stocked at JLCPCB; fit when expansion boards exist
