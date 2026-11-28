# LCSC / JLCPCB part numbers per part value (as in parts.py), looked up in the JLCPCB parts
# catalogue on 2026-11-28 (stock at that date in the comment; "basic" = no extended-part fee).
# Check stock again when ordering: the BOM upload shows any part that ran out.
LCSC = {
    # connectors
    'ST1 EBL': 'C127367',                 # Molex 43045-0600, 34k
    'ST2 FRESH': 'C492352',               # Molex 43650-0600, 1.9k
    'ST3 GREY': 'C492351',                # Molex 43650-0500, 1.8k
    'KF2EDGR-5.0-4P': 'C441195',          # pluggable header 5.0 mm 4p, 1.9k (plug KF2EDGK-5.0-4P C440871)
    'KF2EDGR-5.0-3P': 'C441194',          # pluggable header 5.0 mm 3p, 1.2k (plug KF2EDGK-5.0-3P C440870)
    'KF2EDGR-3.5-4P': 'C441173',          # pluggable header 3.5 mm 4p, 8.9k (plug KF2EDGK-3.5-4P C440849)
    'DS1133-S60BPX': 'C77859',            # Connfly RJ12 6P6C, 15k
    'DS1128-09-S8B8S': 'C77851',          # Connfly RJ45 shielded, 1.6k
    'USB PROG': 'C3020560',               # GCT USB4105-GF-A, 3.2k
    'PD module': 'C492401',               # 2-pin header 2.54 mm, 848k
    # power
    'TSR 1-2450': 'C53183920',            # Traco, 620
    'AP7361C-33E': 'C500795',             # Diodes AP7361C-33E-13 SOT-223, 3.3k
    'AOD4185': 'C400894',                 # AOS P-MOSFET -40 V, 39k
    'SS34': 'C8678',                      # basic
    'SS14': 'C2480',                      # basic
    'SMBJ15A': 'C113988',                 # 81k
    'SMBJ18A': 'C10212',                  # 3.1k
    'BZT52C12': 'C43491',                 # 192k
    '10A 0451010.MRL': 'C44479',          # Littelfuse Nano2 10 A, 18k
    '5A 0451005.MRL': 'C48467',           # Nano2 5 A, 16k
    '4A 0451004.MRL': 'C27515',           # Nano2 4 A, 10k
    '0.5A 30V PTC': 'C7542947',           # 1206L050/30NR, 114k
    '50mA 15V PTC': 'C49318328',          # 1206L005/30V (30 V rated), 9k
    '1A 30V PTC': 'C52748006',            # 1812L110/30GR (1.1 A hold), 4.8k
    # controller, interfaces, sensors
    'ESP32-S3-WROOM-1-N8': 'C2913198',    # 2.7k
    'USBLC6-2SC6': 'C7519',               # 27k
    'TJA1021T/20': 'C128810',             # NXP TJA1021T/20/CM,118, 15k
    'PESD1LIN': 'C58901',                 # Nexperia, 95k
    'LIS3DH': 'C15134',                   # ST LIS3DHTR, 1.3k
    'SHT40-AD1B': 'C2909890',             # Sensirion SHT40-AD1B-R2, 12k
    'ESDA6V1-5SC6': 'C6650',              # ST, 2.9k
    'INA180A3': 'C122882',                # TI INA180A3IDBVR, 60k
    'LMV331': 'C34731',                   # TI LMV331IDBVR SOT-23-5, 205k
    'AQY212EH': 'C29276',                 # Panasonic AQY212EHAX SOP-4, 3.6k
    'PC817': 'C41807',                    # Sharp PC817X3CSP9F SMD, 297k
    'SK6812MINI-HS': 'C2922787',          # 73k
    # relays, switches, LEDs
    'SRD-12VDC-SL-C': 'C30431',           # Songle, 137k
    'AO3400A': 'C20917',                  # basic
    'AO3401A': 'C15127',                  # basic
    '2N7002': 'C8545',                    # basic
    'TS-1187A-B-A-B': 'C318884',          # basic
    'SKHHLQA010': 'C125034',              # Alps right-angle, 1.1k
    'red': 'C2286',                       # 0603, basic
    'green 0603': 'C12624',               # 139k
    '1N4148W': 'C81598',                  # basic
    '4V7': 'C2116',                       # BZT52C4V7
    '2V4': 'C173420',                     # BZT52C2V4
    'ESD 5V': 'C2827694',                 # PESD5V0S1BA, 877k
    # resistors (0603 1 % UNI-ROYAL, basic unless noted)
    '10k': 'C25804', '100k': 'C25803', '1k': 'C21190', '4.7k': 'C23162', '2.2k': 'C4190',
    '330': 'C23138', '100': 'C22775', '22k': 'C31850', '5.1k': 'C23186', '1M': 'C22935',
    '120': 'C22787', '620': 'C23220',     # 620: extended, 201k
    '4.7k 0.25W': 'C17936',               # 1206, basic
    '0.05 2W': 'C5375430',                # 2512 50 mOhm 2 W, 11k
    '1m 2W 2512 (C-feed link)': 'C2904226',  # 2512 1 mOhm 2 W, 17k
    # capacitors (basic)
    '100n': 'C14663', '1u': 'C15849', '10u': 'C15850', '22u': 'C45783',
    '10u/50V': 'C138687',                 # 1210 X7R 50 V, 84k
}
