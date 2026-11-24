#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mh4u_server.py - Editor Item Box MH4U dengan antarmuka web (jalan di Termux)

Pakai:
  python mh4u_server.py                    # lalu upload user1 lewat browser
  python mh4u_server.py user1              # buka file langsung (Simpan menimpa + backup)
  python mh4u_server.py user1 --names itemlist.txt --port 8080
Buka http://127.0.0.1:8080 di browser HP. Tutup game MH4U dulu.
Nama item (ID 0-1936) sudah tertanam; itemlist.txt / --names akan menggantikannya bila ada.
Ikon item dibaca dari folder icon/ (sebelah file ini): MH4U-<Kategori>_Icon_<Warna>.png.
Kategori yang belum punya ikon tampil sebagai lencana teks; tambah ikon baru lalu restart.
Opsi --lan membuka server ke WiFi (siapa pun di jaringan bisa mengedit; hati-hati).

Format save: riset proyek open source mh4edit (Rokumaehn, MIT).
Item box: 1400 slot x 4 byte [ID u16][JUMLAH u16] pada offset 0x166 (setelah dekripsi).
Zenny: u32 little-endian pada offset 0x3C (setelah dekripsi), maksimal 9.999.999.
"""
import argparse, json, os, re, struct, sys, threading, time, zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

KEY = b"blowfish key iorajegqmrna4itjeangmb agmwgtobjteowhv9mope"
SAVE_SIZE, BOX_OFF, BOX_SLOTS, MAX_ID, M32 = 81408, 8 + 0x15E, 1400, 1935, 0xFFFFFFFF
ZENNY_OFF, ZENNY_MAX = 0x3C, 9999999  # zenny: u32 pada offset 0x3C (setelah dekripsi), batas game 9.999.999


# ------------------------------------------------------------ Blowfish -----
def _pi_words(n):
    bits = n * 32
    prec = bits + 64
    one = 1 << prec

    def atan_inv(x):
        x2, term, k, sign = x * x, one // x, 1, -1
        total = term
        while term:
            term //= x2
            k += 2
            total += sign * (term // k)
            sign = -sign
        return total

    pi = 4 * (4 * atan_inv(5) - atan_inv(239))
    frac = (pi - 3 * one) >> (prec - bits)
    return [(frac >> (32 * (n - 1 - i))) & M32 for i in range(n)]


class Blowfish:
    def __init__(self, key):
        w = _pi_words(18 + 1024)
        self.P = w[:18]
        self.S = [w[18 + 256 * i:18 + 256 * (i + 1)] for i in range(4)]
        j = 0
        for i in range(18):
            d = 0
            for _ in range(4):
                d = (d << 8) | key[j % len(key)]
                j += 1
            self.P[i] ^= d
        l = r = 0
        for i in range(0, 18, 2):
            l, r = self._enc(l, r)
            self.P[i], self.P[i + 1] = l, r
        for s in range(4):
            for i in range(0, 256, 2):
                l, r = self._enc(l, r)
                self.S[s][i], self.S[s][i + 1] = l, r

    def _f(self, l):
        S0, S1, S2, S3 = self.S
        return ((((S0[l >> 24] + S1[(l >> 16) & 255]) & M32) ^ S2[(l >> 8) & 255]) + S3[l & 255]) & M32

    def _enc(self, l, r):
        P = self.P
        for i in range(16):
            l ^= P[i]
            r ^= self._f(l)
            l, r = r, l
        l, r = r, l
        return l ^ P[17], r ^ P[16]

    def _dec(self, l, r):
        P = self.P
        for i in range(17, 1, -1):
            l ^= P[i]
            r ^= self._f(l)
            l, r = r, l
        l, r = r, l
        return l ^ P[0], r ^ P[1]

    def _run(self, data, fn):
        out = bytearray(len(data))
        for i in range(0, len(data), 8):
            l, r = fn(*struct.unpack_from("<II", data, i))
            struct.pack_into("<II", out, i, l, r)
        return bytes(out)

    def encrypt(self, d):
        return self._run(d, self._enc)

    def decrypt(self, d):
        return self._run(d, self._dec)


def _xor(buf):
    b = bytearray(buf)
    key = struct.unpack_from("<H", b, 2)[0]
    for i in range(4, len(b), 2):
        v = struct.unpack_from("<H", b, i)[0]
        if key == 0:
            key = 1
        key = (key * 0xB0) % 0xFF53
        struct.pack_into("<H", b, i, v ^ key)
    return b


BF = Blowfish(KEY)


def decrypt_save(raw):
    return _xor(BF.decrypt(raw))


def encrypt_save(plain):
    b = bytearray(plain)
    struct.pack_into("<H", b, 0, 16)
    struct.pack_into("<I", b, 4, sum(b[8:]) & M32)
    return BF.encrypt(bytes(_xor(b)))


def read_zenny(plain):
    return struct.unpack_from("<I", plain, ZENNY_OFF)[0]


def read_box(plain):
    out = []
    for i in range(BOX_SLOTS):
        iid, cnt = struct.unpack_from("<HH", plain, BOX_OFF + i * 4)
        if iid == 0 and cnt == 0:
            break
        out.append([iid, cnt])
    return out


# ------------------------------------------------------------ State --------
NAMES_DATA = r""""0"	"--------"
"1"	"Book of Combos 1"
"2"	"Book of Combos 2"
"3"	"Book of Combos 3"
"4"	"Book of Combos 4"
"5"	"Book of Combos 5"
"6"	"Organizer Guide"
"7"	"Pack Rat Guide"
"8"	"Potion"
"9"	"Mega Potion"
"10"	"Nutrients"
"11"	"Mega Nutrients"
"12"	"Antidote"
"13"	"Immunizer"
"14"	"Dash Juice"
"15"	"Mega Dash Juice"
"16"	"Demondrug"
"17"	"Mega Demondrug"
"18"	"Might Pill"
"19"	"Armorskin"
"20"	"Mega Armorskin"
"21"	"Adamant Pill"
"22"	"Cool Drink"
"23"	"Hot Drink"
"24"	"Cleanser"
"25"	"Deodorant"
"26"	"Psychoserum"
"27"	"Herbal Medicine"
"28"	"Max Potion"
"29"	"Ancient Potion"
"30"	"Catalyst"
"31"	"Energy Drink"
"32"	"Gunpowder"
"33"	"Lifecrystals"
"34"	"Lifepowder"
"35"	"Tranquilizer"
"36"	"Mystery Charm"
"37"	"Shining Charm"
"38"	"Timeworn Charm"
"39"	"Distorted Charm"
"40"	"Powercharm"
"41"	"Powertalon"
"42"	"Armorcharm"
"43"	"Armortalon"
"44"	"Raw Meat"
"45"	"Poisoned Meat"
"46"	"Tinged Meat"
"47"	"Drugged Meat"
"48"	"Rare Steak"
"49"	"Well-done Steak"
"50"	"Burnt Meat"
"51"	"Chilled Meat"
"52"	"Hot Meat"
"53"	"Mosswine Jerky"
"54"	"Boomerang"
"55"	"Throwing Knife"
"56"	"Poison Knife"
"57"	"Sleep Knife"
"58"	"Paralysis Knife"
"59"	"Tranq Knife"
"60"	"Bomb Casing"
"61"	"Paintball"
"62"	"Flash Bomb"
"63"	"Sonic Bomb"
"64"	"Dung"
"65"	"Dung Bomb"
"66"	"Smoke Bomb"
"67"	"Poison Smoke Bomb"
"68"	"Farcaster"
"69"	"Tranq Bomb"
"70"	"Spider Web"
"71"	"Net"
"72"	"Trap Tool"
"73"	"Pitfall Trap"
"74"	"Shock Trap"
"75"	"Small Barrel"
"76"	"Large Barrel"
"77"	"Barrel Bomb S"
"78"	"Barrel Bomb S+"
"79"	"Barrel Bomb L"
"80"	"Barrel Bomb L+"
"81"	"Bounce Bomb"
"82"	"Bounce Bomb+"
"83"	"Felvine Bomb"
"84"	"dummy"
"85"	"Huskberry"
"86"	"Bone Husk"
"87"	"Normal S Lv1"
"88"	"Normal S Lv2"
"89"	"Normal S Lv3"
"90"	"Pierce S Lv1"
"91"	"Pierce S Lv2"
"92"	"Pierce S Lv3"
"93"	"Pellet S Lv1"
"94"	"Pellet S Lv2"
"95"	"Pellet S Lv3"
"96"	"Crag S Lv1"
"97"	"Crag S Lv2"
"98"	"Crag S Lv3"
"99"	"Clust S Lv1"
"100"	"Clust S Lv2"
"101"	"Clust S Lv3"
"102"	"Flaming S"
"103"	"Water S"
"104"	"Thunder S"
"105"	"Freeze S"
"106"	"Dragon S"
"107"	"Poison S Lv1"
"108"	"Poison S Lv2"
"109"	"Para S Lv1"
"110"	"Para S Lv2"
"111"	"Sleep S Lv1"
"112"	"Sleep S Lv2"
"113"	"Exhaust S Lv1"
"114"	"Exhaust S Lv2"
"115"	"Slicing S"
"116"	"WyvernFire"
"117"	"Blast S"
"118"	"Recover S Lv1"
"119"	"Recover S Lv2"
"120"	"Demon S"
"121"	"Armor S"
"122"	"Paint S"
"123"	"Tranq S"
"124"	"(No Coating)"
"125"	"Empty Phial"
"126"	"Power Coating"
"127"	"C.Range Coating"
"128"	"Poison Coating"
"129"	"Para Coating"
"130"	"Sleep Coating"
"131"	"Exhaust Coating"
"132"	"Blast Coating"
"133"	"Paint Coating"
"134"	"Power Nectar: Fire"
"135"	"Stam Nectar: Water"
"136"	"Spd Nectar: Thunder"
"137"	"Speed Nectar: Ice"
"138"	"Stam Nectar: Dragon"
"139"	"Power+ Nectar"
"140"	"Stamina+ Nectar"
"141"	"Speed+ Nectar"
"142"	"Nectar: Fire+"
"143"	"Nectar: Water+"
"144"	"Nectar: Thunder+"
"145"	"Nectar: Ice+"
"146"	"Nectar: Dragon+"
"147"	"Power+ Nectar: Fire"
"148"	"Stam+ Nectar: Water"
"149"	"Spd+ Nectar: Thunder"
"150"	"Power Nectar: Fire+"
"151"	"Speed Nectar: Water+"
"152"	"Spd Nectar: Thunder+"
"153"	"Speed Nectar: Ice+"
"154"	"Stam Nectar: Dragon+"
"155"	"Worm"
"156"	"Mega Fishing Fly"
"157"	"Frog"
"158"	"Sushifish Bait"
"159"	"Burst Bait"
"160"	"Goldenfish Bait"
"161"	"Old Pickaxe"
"162"	"Iron Pickaxe"
"163"	"Mega Pickaxe"
"164"	"Old Bug Net"
"165"	"Bug Net"
"166"	"Mega Bug Net"
"167"	"BBQ Spit"
"168"	"Whetstone"
"169"	"Binoculars"
"170"	"Garbage"
"171"	"Field Horn"
"172"	"Health Horn"
"173"	"Antidote Horn"
"174"	"Demon Horn"
"175"	"Armor Horn"
"176"	"Honey"
"177"	"Herb"
"178"	"Antidote Herb"
"179"	"Fire Herb"
"180"	"Ivy"
"181"	"Sleep Herb"
"182"	"Sap Plant"
"183"	"Felvine"
"184"	"Gloamgrass Root"
"185"	"Hot Pepper"
"186"	"Cathangea Seed"
"187"	"Cathangea"
"188"	"Cathangea Elixir"
"189"	"Blue Mushroom"
"190"	"Nitroshroom"
"191"	"Parashroom"
"192"	"Toadstool"
"193"	"Exciteshroom"
"194"	"Mopeshroom"
"195"	"Dragon Toadstool"
"196"	"Paintberry"
"197"	"Might Seed"
"198"	"Adamant Seed"
"199"	"Nulberry"
"200"	"Dragonfell Berry"
"201"	"Scatternut"
"202"	"Needleberry"
"203"	"Latchberry"
"204"	"Bomberry"
"205"	"Bumblepumpkin"
"206"	"Stone"
"207"	"Iron Ore"
"208"	"Earth Crystal"
"209"	"Machalite Ore"
"210"	"Dragonite Ore"
"211"	"Lightcrystal"
"212"	"Ice Crystal"
"213"	"Firestone"
"214"	"Gossamite Ore"
"215"	"Heavenly Crystal"
"216"	"Carbalite Ore"
"217"	"Novacrystal"
"218"	"Fucium Ore"
"219"	"Firecell Stone"
"220"	"Yoldspar Ore"
"221"	"Abrasive"
"222"	"Quality Abrasive"
"223"	"Awesome Abrasive"
"224"	"Armor Stone"
"225"	"Armor Sphere"
"226"	"Armor Sphere+"
"227"	"Adv Armor Sphere"
"228"	"Hard Armor Sphere"
"229"	"Heavy Armor Sphere"
"230"	"True Armor Sphere"
"231"	"Whetfish"
"232"	"Sushifish"
"233"	"Sleepyfish"
"234"	"Pin Tuna"
"235"	"Speartuna"
"236"	"Popfish"
"237"	"Scatterfish"
"238"	"Burst Arowana"
"239"	"Bomb Arowana"
"240"	"Glutton Tuna"
"241"	"Gastronome Tuna"
"242"	"Small Goldenfish"
"243"	"Wanchovy"
"244"	"Ancient Fish"
"245"	"Blue Cutthroat"
"246"	"Bronze Guild Ticket"
"247"	"Silver Guild Ticket"
"248"	"Insect Husk"
"249"	"Gold Guild Ticket"
"250"	"Stinkhopper"
"251"	"Snakebee Larva"
"252"	"Godbug"
"253"	"Bitterbug"
"254"	"Flashbug"
"255"	"Thunderbug"
"256"	"Glueglopper"
"257"	"Killer Beetle"
"258"	"Hercudrome"
"259"	"Fulgurbug"
"260"	"Rare Scarab"
"261"	"Great Hornfly"
"262"	"Butterfly Beetle"
"263"	"Monster Bone S"
"264"	"Monster Bone M"
"265"	"Monster Bone L"
"266"	"Mystery Bone"
"267"	"Unknown Skull"
"268"	"Monster Bone+"
"269"	"Monster Keenbone"
"270"	"Monster Hardbone"
"271"	"Elder Dragon Bone"
"272"	"Avian Finebone"
"273"	"Brute Bone"
"274"	"Jumbo Bone"
"275"	"Stoutbone"
"276"	"Dragonbone Relic"
"277"	"Wyvern Fang"
"278"	"Wyvern Claw"
"279"	"Bird Wyvern Gem"
"280"	"Wyvern Gem"
"281"	"Elder Dragon Blood"
"282"	"Steel Egg"
"283"	"Silver Egg"
"284"	"Golden Egg"
"285"	"Slickaxe"
"286"	"Screamer Sac"
"287"	"Poison Sac"
"288"	"Toxin Sac"
"289"	"Paralysis Sac"
"290"	"Sleep Sac"
"291"	"Flame Sac"
"292"	"Inferno Sac"
"293"	"Electro Sac"
"294"	"Thunder Sac"
"295"	"Frost Sac"
"296"	"Freezer Sac"
"297"	"Aqua Sac"
"298"	"Torrent Sac"
"299"	"Dash Extract"
"300"	"Pale Extract"
"301"	"Gargwa Feather"
"302"	"Kelbi Horn"
"303"	"Blue Kelbi Horn"
"304"	"Warm Pelt"
"305"	"High-quality Pelt"
"306"	"Rhenoplos Shell"
"307"	"Rhenoplos Carapace"
"308"	"Rhenoplos Scalp"
"309"	"Slagtoth Hide"
"310"	"Slagtoth Hide+"
"311"	"Bnahabra Shell"
"312"	"Bnahabra Carapace"
"313"	"Bnahabra Wing"
"314"	"Altaroth Jaw"
"315"	"Altaroth Stomach"
"316"	"Quality Stomach"
"317"	"Sharp Fang"
"318"	"Konchu Shell"
"319"	"Konchu Carapace"
"320"	"Remobra Hide"
"321"	"Remobra Hide+"
"322"	"Striped Skin"
"323"	"Remobra Head"
"324"	"Jaggi Scale"
"325"	"Jaggi Scale+"
"326"	"Jaggi Hide"
"327"	"Bird Wyvern Fang"
"328"	"Tough Bird Wyv Hide"
"329"	"G.Jaggi Hide"
"330"	"G.Jaggi Hide+"
"331"	"G.Jaggi Claw"
"332"	"G.Jaggi Claw+"
"333"	"King's Frill"
"334"	"Great Jaggi Head"
"335"	"Velociprey Scale"
"336"	"Velociprey Scale+"
"337"	"Velociprey Hide"
"338"	"Velociprey Hide+"
"339"	"Velocidrome Hide"
"340"	"Velocidrome Claw"
"341"	"Velocidrome Claw+"
"342"	"Velocidrome Head"
"343"	"Genprey Fang"
"344"	"Genprey Scale"
"345"	"Genprey Scale+"
"346"	"Genprey Hide"
"347"	"Genprey Hide+"
"348"	"Gendrome Hide"
"349"	"Gendrome Tail"
"350"	"Gendrome Head"
"351"	"Ioprey Fang"
"352"	"Ioprey Scale"
"353"	"Ioprey Scale+"
"354"	"Ioprey Hide"
"355"	"Ioprey Hide+"
"356"	"Iodrome Hide"
"357"	"Iodrome Head"
"358"	"Lagombi Pelt"
"359"	"Lagombi Pelt+"
"360"	"Lagombi Ear"
"361"	"Lagombi Ear+"
"362"	"Lagombi Plastron"
"363"	"Lagombi Plastron+"
"364"	"Lagombi Iceclaw"
"365"	"Lagombi Frozenclaw"
"366"	"Kut-Ku Scale"
"367"	"Kut-Ku Scale+"
"368"	"Kut-Ku Shell"
"369"	"Kut-Ku Carapace"
"370"	"Kut-Ku Ear"
"371"	"Kut-Ku Auricle"
"372"	"Giant Beak"
"373"	"Splendid Beak"
"374"	"Kut-Ku Webbing"
"375"	"Invalid Message"
"376"	"Blue Kut-Ku Scale+"
"377"	"Blue Kut-Ku Carapace"
"378"	"Rubbery Hide"
"379"	"Rubbery Hide+"
"380"	"Gypceros Head"
"381"	"Rubbery Purple Hide"
"382"	"P.Gypceros Wing"
"383"	"Garuga Scale"
"384"	"Garuga Scale+"
"385"	"Garuga Shell"
"386"	"Garuga Carapace"
"387"	"Garuga Ear"
"388"	"Garuga Auricle"
"389"	"Sharpened Beak"
"390"	"Rejuvenated Beak"
"391"	"Garuga Wing"
"392"	"Garuga Tail"
"393"	"Garuga Mane"
"394"	"Kecha Pelt"
"395"	"Kecha Pelt+"
"396"	"Kecha Longbone"
"397"	"Kecha Grandbone"
"398"	"Kecha Talon"
"399"	"Kecha Ear"
"400"	"Kecha Ear+"
"401"	"Kecha Tail"
"402"	"Conga Pelt"
"403"	"Conga Pelt+"
"404"	"Sharp Claw"
"405"	"Congalala Pelt"
"406"	"Congalala Pelt+"
"407"	"Vibrant Pelt"
"408"	"Congalala Claw"
"409"	"Congalala Claw+"
"410"	"Congalala Fang"
"411"	"E.Congalala Pelt+"
"412"	"E.Congalala Fang+"
"413"	"E.Congalala Claw+"
"414"	"Rajang Horn+"
"415"	"Rajang Fang+"
"416"	"Rajang Blackfur"
"417"	"Rajang Claw+"
"418"	"Rajang Tail"
"419"	"Gold Rajang Pelt"
"420"	"Rajang Ragehair"
"421"	"Rajang Nerve"
"422"	"Ghoulish Gold Horn"
"423"	"Tetsucabra Scale"
"424"	"Tetsucabra Scale+"
"425"	"Tetsucabra Shell"
"426"	"Tetsucabra Carapace"
"427"	"Tetsucabra Claw"
"428"	"Tetsucabra Claw+"
"429"	"Tetsucabra Tusks"
"430"	"Tetsucabra Tusks+"
"431"	"Dignified Skull"
"432"	"Paddock Oil"
"433"	"Paddock Oil+"
"434"	"Sharqskin Scale"
"435"	"Meaty Hide"
"436"	"Meaty Hide+"
"437"	"Zamtrios Hide"
"438"	"Zamtrios Hide+"
"439"	"Zamtrios Scale"
"440"	"Zamtrios Scale+"
"441"	"Zamtrios Fin"
"442"	"Zamtrios Fin+"
"443"	"Zamtrios Sawtooth"
"444"	"Zamtrios Tailblade"
"445"	"Najarala Shell"
"446"	"Najarala Carapace"
"447"	"Najarala Hide"
"448"	"Najarala Hide+"
"449"	"Najarala Marrow"
"450"	"Najarala Medulla"
"451"	"Najarala Sounder"
"452"	"Najarala Sounder+"
"453"	"Najarala Fang"
"454"	"Chilling Beak"
"455"	"Nerscylla Shell"
"456"	"Nerscylla Carapace"
"457"	"Nerscylla Chelicera"
"458"	"Nerscylla Shear"
"459"	"Nerscylla Claw"
"460"	"Nerscylla Sharpclaw"
"461"	"Nerscylla Spike"
"462"	"Nerscylla Spike+"
"463"	"Monster Fluid"
"464"	"Monster Broth"
"465"	"Seltas Shell"
"466"	"Seltas Carapace"
"467"	"Seltas Wing"
"468"	"Seltas Innerwing"
"469"	"Seltas Horn"
"470"	"Seltas Horn+"
"471"	"S.Queen Shell"
"472"	"S.Queen Carapace"
"473"	"S.Queen Pectus"
"474"	"S.Queen Hardpectus"
"475"	"S.Queen Claw"
"476"	"S.Queen Sharpclaw"
"477"	"S.Queen Jaw"
"478"	"S.Queen Crescent"
"479"	"S.Queen Extract"
"480"	"S.Queen Concentrate"
"481"	"Flabby Hide"
"482"	"Pearl Hide"
"483"	"Pale Bone"
"484"	"Pale Steak"
"485"	"Alluring Hide"
"486"	"Alluring Webbing"
"487"	"Rathian Scale"
"488"	"Rathian Scale+"
"489"	"Rathian Shell"
"490"	"Rathian Carapace"
"491"	"Rathian Webbing"
"492"	"Rathian Spike"
"493"	"Rathian Spike+"
"494"	"Rathian Plate"
"495"	"Rathian Ruby"
"496"	"P.Rathian Scale+"
"497"	"P.Rathian Carapace"
"498"	"G.Rathian Scale+"
"499"	"G.Rathian Carapace"
"500"	"G.Rathian Spike+"
"501"	"Rathalos Scale"
"502"	"Rathalos Scale+"
"503"	"Rathalos Shell"
"504"	"Rathalos Carapace"
"505"	"Rathalos Webbing"
"506"	"Rathalos Wing"
"507"	"Rathalos Tail"
"508"	"Rathalos Plate"
"509"	"Rathalos Ruby"
"510"	"A.Rathalos Scale+"
"511"	"A.Rathalos Carapace"
"512"	"A.Rathalos Tail"
"513"	"A.Rathalos Wing"
"514"	"S.Rathalos Scale+"
"515"	"S.Rathalos Carapace"
"516"	"S.Rathalos Tail"
"517"	"Rath Wingtalon"
"518"	"Rath Marrow"
"519"	"Rath Medulla"
"520"	"Rath Gleam"
"521"	"Zinogre Shell"
"522"	"Zinogre Carapace"
"523"	"Zinogre Shockfur"
"524"	"Zinogre Electrofur"
"525"	"Zinogre Horn"
"526"	"Zinogre Horn+"
"527"	"Zinogre Claw"
"528"	"Zinogre Claw+"
"529"	"Zinogre Tail"
"530"	"Zinogre Shocker"
"531"	"Zinogre Shocker+"
"532"	"Zinogre Plate"
"533"	"Zinogre Jasper"
"534"	"S.Zinogre Carapace"
"535"	"S.Zinogre Horn+"
"536"	"S.Zinogre Claw+"
"537"	"S.Zinogre Tail"
"538"	"S.Zin Drakeshell"
"539"	"Dracophage Bug"
"540"	"S.Zinogre Dragonhair"
"541"	"S.Zinogre Umbrage"
"542"	"Brach Shell"
"543"	"Brach Carapace"
"544"	"Brach Ebonshell"
"545"	"Brach Ebonshell+"
"546"	"Brach Scalp"
"547"	"Brach Scalp+"
"548"	"Mystery Slime"
"549"	"Brach Hammer"
"550"	"Brach Pounder"
"551"	"Brach Tail"
"552"	"Brach Marrow"
"553"	"Brach Gem"
"554"	"Basarios Shell"
"555"	"Basarios Carapace"
"556"	"Basarios Wing"
"557"	"Basarios Tears"
"558"	"R.Basarios Wing"
"559"	"R.Basarios Carapace"
"560"	"Gravios Shell"
"561"	"Gravios Carapace"
"562"	"Gravios Scalp"
"563"	"Gravios Medulla"
"564"	"Gravios Wing"
"565"	"B.Gravios Carapace"
"566"	"B.Gravios Scalp"
"567"	"Tigrex Scale"
"568"	"Tigrex Scale+"
"569"	"Tigrex Shell"
"570"	"Tigrex Carapace"
"571"	"Tigrex Tail"
"572"	"Tigrex Claw"
"573"	"Tigrex Claw+"
"574"	"Tigrex Fang"
"575"	"Tigrex Fang+"
"576"	"Tigrex Scalp"
"577"	"Tigrex Maw"
"578"	"B.Tigrex Scale+"
"579"	"B.Tigrex Carapace"
"580"	"B.Tigrex Claw+"
"581"	"B.Tigrex Fang+"
"582"	"M.Tigrex Scale+"
"583"	"M.Tigrex Carapace"
"584"	"M.Tigrex Blastclaw"
"585"	"M.Tigrex Fang+"
"586"	"M.Tigrex Tail"
"587"	"Pulsating Blastheart"
"588"	"Gore Magala Scale"
"589"	"Gore Magala Scale+"
"590"	"Gore Magala Shell"
"591"	"Gore Magala Carapace"
"592"	"Gore Magala Wing"
"593"	"Gore Magala Wing+"
"594"	"Gore Magala Ripclaw"
"595"	"Gore Magala Ripclaw+"
"596"	"Gore Magala Tail"
"597"	"Gore Magala Plate"
"598"	"Gore Magala Nyctgem"
"599"	"Gore Magala Feeler"
"600"	"Gore Magala Feeler+"
"601"	"Defiled Scale"
"602"	"S.Magala Scale"
"603"	"S.Magala Scale+"
"604"	"S.Magala Shell"
"605"	"S.Magala Carapace"
"606"	"S.Magala Prismwing"
"607"	"S.Magala Prismwing+"
"608"	"S.Magala Caress"
"609"	"S.Magala Caress+"
"610"	"S.Magala Tail"
"611"	"S.Magala Plate"
"612"	"S.Magala Phosgem"
"613"	"S.Magala Horn"
"614"	"S.Magala Horn+"
"615"	"Pure Scale"
"616"	"Deviljho Scale"
"617"	"Deviljho Hide"
"618"	"Deviljho Tallfang"
"619"	"Deviljho Talon"
"620"	"Deviljho Scalp"
"621"	"Deviljho Tail"
"622"	"Deviljho Saliva"
"623"	"Black Blood"
"624"	"Spattered Hide"
"625"	"Deviljho Gem"
"626"	"Kirin Thunderhorn"
"627"	"Kirin Hide"
"628"	"Kirin Hide+"
"629"	"Kirin Mane"
"630"	"Kirin Thundertail"
"631"	"Kirin Azure Horn"
"632"	"Kirin Icehorn"
"633"	"Kirin Frost Tail"
"634"	"Daora Scale"
"635"	"Daora Carapace"
"636"	"Daora Claw+"
"637"	"Daora Tail"
"638"	"Daora Webbing"
"639"	"Daora Horn+"
"640"	"Daora Gem"
"641"	"Teostra Carapace"
"642"	"Teostra Mane"
"643"	"Teostra Tail"
"644"	"Teostra Horn+"
"645"	"Fire Dragon Scale+"
"646"	"Teostra Claw+"
"647"	"Teostra Webbing"
"648"	"Teostra Powder"
"649"	"Teostra Gem"
"650"	"D.Mohran Shell"
"651"	"D.Mohran Carapace"
"652"	"D.Mohran Bloodscale"
"653"	"D.Mohran Bloodscale+"
"654"	"D.Mohran Brace"
"655"	"D.Mohran Brace+"
"656"	"Brutal Horn"
"657"	"Brutal Horn+"
"658"	"Dah'renstone"
"659"	"Earth Dragongem"
"660"	"Akantor Scale+"
"661"	"Akantor Carapace"
"662"	"Akantor Tail"
"663"	"Akantor Hardclaw"
"664"	"Akantor Tallfang"
"665"	"Akantor Spike"
"666"	"Akantor Gem"
"667"	"Fatalis Horn"
"668"	"Fatalis Scale"
"669"	"Fatalis Shell"
"670"	"Fatalis Eye"
"671"	"Fatalis Webbing"
"672"	"Fatalis Bloodhorn"
"673"	"Fatalis Bloodscale"
"674"	"Fatalis Bloodshell"
"675"	"Fatalis Devileye"
"676"	"Fatalis Bloodwing"
"677"	"Dalamadur Steel"
"678"	"Dalam Tail Scale"
"679"	"Dalamadur Gazer"
"680"	"Dalamadur Pectus"
"681"	"Dalamadur Glaive"
"682"	"Dalamadur Talon"
"683"	"Dalam Tail Shell"
"684"	"Dalamadur Fanblade"
"685"	"Vortex Bone"
"686"	"Skyblade Gem"
"687"	"Shattered Omen"
"688"	"Frenzy Shard"
"689"	"Frenzy Crystal"
"690"	"Vile Frenzy Crystal"
"691"	"Small Lobstershell"
"692"	"Lobster Tail Fan"
"693"	"Plesioth Scale"
"694"	"Plesioth Fin"
"695"	"Arzuros Shell"
"696"	"Arzuros Pelt"
"697"	"Royal Ludroth Claw"
"698"	"Spongy Hide"
"699"	"Vivid Feather"
"700"	"Strange Beak"
"701"	"Barroth Shell"
"702"	"Barroth Scalp"
"703"	"Nibelsnarf Hide"
"704"	"Vivid Fluid"
"705"	"Ceanataur Shell"
"706"	"Ceanataur Pincer"
"707"	"Uragaan Marrow"
"708"	"Uragaan Scute"
"709"	"Nargacuga Blackfur"
"710"	"Nargacuga Marrow"
"711"	"Nargacuga Cutwing"
"712"	"Duramboros Mossplate"
"713"	"Duramboros Horn"
"714"	"Lavasioth Scale"
"715"	"Lavasioth Fin"
"716"	"Diablos Shell"
"717"	"Majestic Horn"
"718"	"Lao-Shan Shell"
"719"	"Lao-Shan Claw"
"720"	"Lao-Shan Horn"
"721"	"Vespoid Innerwing"
"722"	"Cephalos Fin+"
"723"	"Cephalos Scale+"
"724"	"Hermitaur Carapace"
"725"	"Hermitaur Claw+"
"726"	"Gobul Hide+"
"727"	"Gobul Spike+"
"728"	"Volvidon Carapace"
"729"	"Amber Tusks+"
"730"	"Barioth Pelt+"
"731"	"Blangonga Pelt+"
"732"	"Blangonga Whisker"
"733"	"Blangonga Fang+"
"734"	"Shell Shocker+"
"735"	"Lagiacrus Sapphire"
"736"	"Agnaktor Carapace"
"737"	"Agnaktor Fin+"
"738"	"Lunastra Carapace"
"739"	"Lunastra Mane"
"740"	"Chameleos Hide+"
"741"	"Chameleos Gem"
"742"	"Chameleos Tail"
"743"	"Tsukami Fluid"
"744"	"Dragonwood+"
"745"	"Dragonmoss+"
"746"	"Ukanlos Shoveljaw"
"747"	"Ukanlos Carapace"
"748"	"Ukanlos Gem"
"749"	"Gaoren Pincer"
"750"	"Gaoren Thoracic"
"751"	"Gaoren Claw+"
"752"	"Ceadeus Hide"
"753"	"Crooked Horn"
"754"	"Luminous Organ"
"755"	"Alatreon Plate"
"756"	"Alatreon Webbing"
"757"	"Skypiercer"
"758"	"Rustshard"
"759"	"Ancient Shard"
"760"	"Rusted Fragment"
"761"	"Ancient Fragment"
"762"	"Battered Weapon"
"763"	"Battered Armor"
"764"	"Seasoned Weapon"
"765"	"Seasoned Armor"
"766"	"Venerable Weapon"
"767"	"Venerable Armor"
"768"	"Map"
"769"	"First-aid Med"
"770"	"First-aid Med+"
"771"	"Ration"
"772"	"Portable Spit"
"773"	"Mini Whetstone"
"774"	"EZ Shock Trap"
"775"	"EZ Flash Bomb"
"776"	"EZ Sonic Bomb"
"777"	"EZ Max Potion"
"778"	"EZ Pitfall Trap"
"779"	"EZ Barrel Bomb L"
"780"	"Ballista Ammo"
"781"	"Ballista Binder"
"782"	"One-shot Binder"
"783"	"Cannon Ammo"
"784"	"Anti-dragon Bomb"
"785"	"Torch"
"786"	"Paw Pass Ticket"
"787"	"Unique Mushroom"
"788"	"Choice Mushroom"
"789"	"Ripened Mushroom"
"790"	"Goldenfish"
"791"	"Silverfish"
"792"	"Royal Rhino"
"793"	"Sulfur Crystal"
"794"	"Auristone Chunk"
"795"	"Crystal Bone"
"796"	"Bloodstone"
"797"	"Coal"
"798"	"Fossilized Bone"
"799"	"Mistentoe"
"800"	"Decayed Scale"
"801"	"Tiny Spall"
"802"	"Lucky Spall"
"803"	"Miracle Spall"
"804"	"White Liver"
"805"	"Slagtoth Oil"
"806"	"Secret Stash"
"807"	"Round Acorn"
"808"	"Fine Stomach"
"809"	"Luminous Stomach"
"810"	"Monster Guts"
"811"	"Popo Tongue"
"812"	"Gargwa Egg"
"813"	"Gold Gargwa Egg"
"814"	"Wyvern Egg"
"815"	"Herbivore Egg"
"816"	"Sootstone Ore"
"817"	"Powderstone"
"818"	"Gargwa Guano"
"819"	"Territorial Dung"
"820"	"Territorial Dung+"
"821"	"Drone Substance"
"822"	"Queen Substance"
"823"	"Toad Tear"
"824"	"Large Toad Tear"
"825"	"Wyvern Tear"
"826"	"Large Wyvern Tear"
"827"	"Beast Tear"
"828"	"Large Beast Tear"
"829"	"Dragon Treasure"
"830"	"Old Dragon Treasure"
"831"	"Pinnacle Coin"
"832"	"Hunter King Coin"
"833"	"Ace Hunter Coin"
"834"	"Beast Coin"
"835"	"Flying Coin"
"836"	"Bird Coin"
"837"	"Fang Coin"
"838"	"Brute Coin"
"839"	"Vermin Coin"
"840"	"Toad Coin"
"841"	"Invalid Message"
"842"	"Voucher"
"843"	"Gourmet Voucher"
"844"	"Studio Pass"
"845"	"dummy"
"846"	"dummy"
"847"	"dummy"
"848"	"dummy"
"849"	"dummy"
"850"	"dummy"
"851"	"dummy"
"852"	"Fan Club Invite"
"853"	"Monqlo Ticket"
"854"	"Gold Studio Pass"
"855"	"Platinum Pass"
"856"	"dummy"
"857"	"Amulet Keepsake"
"858"	"dummy"
"859"	"dummy"
"860"	"Interpol Badge"
"861"	"Val Habar Ticket"
"862"	"dummy"
"863"	"Goddess's Melody"
"864"	"Goddess's Embrace"
"865"	"dummy"
"866"	"Uniqlo Ticket"
"867"	"dummy"
"868"	"dummy"
"869"	"dummy"
"870"	"Anklet Memento"
"871"	"dummy"
"872"	"dummy"
"873"	"dummy"
"874"	"Fan Club Certificate"
"875"	"Triforce"
"876"	"Goddess's Fire"
"877"	"Super Mushroom"
"878"	"Invalid Message"
"879"	"D.Mohran Ticket"
"880"	"Invalid Message"
"881"	"Dalamadur Ticket"
"882"	"Rajang Ticket"
"883"	"Gore Magala Ticket"
"884"	"Viper & Toad Ticket"
"885"	"Oroshi Kirin Ticket"
"886"	"Commendation"
"887"	"Commendation G"
"888"	"Accolade"
"889"	"Accolade G"
"890"	"Caravan Ticket"
"891"	"Palico Ticket"
"892"	"Meownster Ticket"
"893"	"Quester's Ticket"
"894"	"Kushala Daora Ticket"
"895"	"Teostra Ticket"
"896"	"Pawprint Stamp"
"897"	"VE Ticket"
"898"	"VE Deluxe Ticket"
"899"	"VE Bronze Ticket"
"900"	"VE Silver Ticket"
"901"	"VE Golden Ticket"
"902"	"Humble Scrap"
"903"	"Handsome Scrap"
"904"	"Gargwa Scrap"
"905"	"Arzuros Scrap"
"906"	"Kecha Wacha Scrap"
"907"	"Yian Kut-Ku Scrap"
"908"	"Bnahabra Scrap"
"909"	"Bulldrome Scrap"
"910"	"Rhenoplos Scrap"
"911"	"Great Jaggi Scrap"
"912"	"Nerscylla Scrap"
"913"	"Qurupeco Scrap"
"914"	"Zamtrios Scrap"
"915"	"Lagombi Scrap"
"916"	"Tetsucabra Scrap"
"917"	"Barroth Scrap"
"918"	"Slagtoth Scrap"
"919"	"Volvidon Scrap"
"920"	"Tigrex Scrap"
"921"	"Great Baggi Scrap"
"922"	"Nibelsnarf Scrap"
"923"	"Seltas Queen Scrap"
"924"	"Uragaan Scrap"
"925"	"Great Wroggi Scrap"
"926"	"Diablos Scrap"
"927"	"Rathian Scrap"
"928"	"Khezu Scrap"
"929"	"Duramboros Scrap"
"930"	"Zinogre Scrap"
"931"	"Nargacuga Scrap"
"932"	"Rathalos Scrap"
"933"	"Agnaktor Scrap"
"934"	"Najarala Scrap"
"935"	"Gore Magala Scrap"
"936"	"Shagaru Magala Scrap"
"937"	"Teostra Scrap"
"938"	"Deviljho Scrap"
"939"	"Akantor Scrap"
"940"	"Alatreon Scrap"
"941"	"Kushala Daora Scrap"
"942"	"Ukanlos Scrap"
"943"	"Kirin Scrap"
"944"	"Gigginox Scrap"
"945"	"Rajang Scrap"
"946"	"Brach Scrap"
"947"	"Barioth Scrap"
"948"	"Amatsu Scrap"
"949"	"Dah'ren Mohran Scrap"
"950"	"Jhen Mohran Scrap"
"951"	"Dalamadur Scrap"
"952"	"Royal Ludroth Scrap"
"953"	"Plesioth Scrap"
"954"	"Lagiacrus Scrap"
"955"	"Aquaglow Jewel"
"956"	"Sunspire Jewel"
"957"	"Bloodrun Jewel"
"958"	"Lazurite Jewel"
"959"	"Adamant Orb"
"960"	"Antidote Jewel 1"
"961"	"Antidote Jewel 2"
"962"	"Paralysis Jwl 1"
"963"	"Paralysis Jwl 2"
"964"	"Pep Jewel 1"
"965"	"Pep Jewel 2"
"966"	"Steadfast Jwl 1"
"967"	"Standfast Jwl 1"
"968"	"Tracker Jewel 1"
"969"	"Trekker Jewel 1"
"970"	"Ninja Jewel 1"
"971"	"Vitality Jewel 1"
"972"	"Recovery Jewel 1"
"973"	"Recovery Jewel 2"
"974"	"Razor Jewel 1"
"975"	"Razor Jewel 3"
"976"	"Handicraft Jwl 2"
"977"	"Handicraft Jwl 3"
"978"	"Fencer Jewel 1"
"979"	"Fencer Jewel 3"
"980"	"Expert Jewel 1"
"981"	"Expert Jewel 2"
"982"	"Expert Jewel 3"
"983"	"Grinder Jewel 1"
"984"	"Ironwall Jewel 1"
"985"	"Ironwall Jewel 2"
"986"	"Shield Jewel 1"
"987"	"Shield Jewel 2"
"988"	"Quickload Jwl 1"
"989"	"Quickload Jwl 2"
"990"	"Absorber Jewel 1"
"991"	"Absorber Jewel 3"
"992"	"Forceshot Jwl 1"
"993"	"Forceshot Jwl 3"
"994"	"Pierce Jewel 1"
"995"	"Pierce Jewel 3"
"996"	"Pellet Jewel 1"
"997"	"Pellet Jewel 3"
"998"	"ShotPlus Jewel 1"
"999"	"ShotPlus Jewel 2"
"1000"	"PiercePlus Jwl 1"
"1001"	"PelletPlus Jwl 1"
"1002"	"CragPlus Jewel 1"
"1003"	"ClustPlus Jwl 1"
"1004"	"Disabler Jewel 1"
"1005"	"Disabler Jewel 2"
"1006"	"Element Jewel 1"
"1007"	"Element Jewel 3"
"1008"	"Bombardier Jwl 1"
"1009"	"Hungerless Jwl 1"
"1010"	"Metabolism Jwl 1"
"1011"	"Attack Jewel 1"
"1012"	"Attack Jewel 2"
"1013"	"Attack Jewel 3"
"1014"	"Defense Jewel 1"
"1015"	"Defense Jewel 2"
"1016"	"Protection Jwl 1"
"1017"	"Asylum Jewel 1"
"1018"	"Earplug Jewel 1"
"1019"	"Earplug Jewel 3"
"1020"	"Alarm Jewel 1"
"1021"	"Friendship Jwl 1"
"1022"	"Friendship Jwl 2"
"1023"	"Transportr Jwl 1"
"1024"	"Fire Res Jewel 1"
"1025"	"Water Res Jwl 1"
"1026"	"Ice Res Jewel 1"
"1027"	"ThunderRes Jwl 1"
"1028"	"Dragon Res Jwl 1"
"1029"	"Heat Res Jewel 1"
"1030"	"Cold Res Jewel 1"
"1031"	"Wind Res Jewel 1"
"1032"	"Wind Res Jewel 2"
"1033"	"Gathering Jwl 1"
"1034"	"Spree Jewel 1"
"1035"	"Blessing Jewel 1"
"1036"	"Fate Jewel 1"
"1037"	"Fate Jewel 3"
"1038"	"Psychic Jewel 1"
"1039"	"Medicine Jewel 1"
"1040"	"Medicine Jewel 2"
"1041"	"Professor Jwl 1"
"1042"	"Factory Jewel 1"
"1043"	"Evasion Jewel 1"
"1044"	"Evasion Jewel 2"
"1045"	"Potential Jwl 1"
"1046"	"Potential Jwl 2"
"1047"	"Enduring Jewel 1"
"1048"	"Sprinter Jewel 1"
"1049"	"Sprinter Jewel 3"
"1050"	"Capacity Jewel 2"
"1051"	"Capacity Jewel 3"
"1052"	"Sniper Jewel 1"
"1053"	"Gobbler Jewel 1"
"1054"	"Gobbler Jewel 3"
"1055"	"Carver Jewel 1"
"1056"	"Carver Jewel 3"
"1057"	"Footing Jewel 1"
"1058"	"Tectonic Jewel 1"
"1059"	"Jumping Jewel 1"
"1060"	"Draw Jewel 1"
"1061"	"Draw Jewel 2"
"1062"	"Trapmaster Jwl 1"
"1063"	"Physique Jewel 1"
"1064"	"Physique Jewel 2"
"1065"	"Capture Jewel 1"
"1066"	"Capture Jewel 3"
"1067"	"Perception Jwl 1"
"1068"	"Charger Jewel 1"
"1069"	"Charger Jewel 3"
"1070"	"Gambit Jewel 1"
"1071"	"Gambit Jewel 3"
"1072"	"Resistor Jewel 1"
"1073"	"Release Jewel 2"
"1074"	"Catalyst Jewel 3"
"1075"	"Fortitude Jwl 1"
"1076"	"Fortitude Jwl 2"
"1077"	"Salvo Jewel 1"
"1078"	"Salvo Jewel 3"
"1079"	"Torchlight Jwl 1"
"1080"	"Def Lock Jewel 1"
"1081"	"Blaze Jewel 1"
"1082"	"Blaze Jewel 2"
"1083"	"Stream Jewel 1"
"1084"	"Stream Jewel 2"
"1085"	"Bolt Jewel 1"
"1086"	"Bolt Jewel 2"
"1087"	"Frost Jewel 1"
"1088"	"Frost Jewel 2"
"1089"	"Dragon Jewel 1"
"1090"	"Dragon Jewel 2"
"1091"	"Tenderizer Jwl 1"
"1092"	"Tenderizer Jwl 3"
"1093"	"Destroyer Jwl 1"
"1094"	"Destroyer Jwl 3"
"1095"	"Throttle Jewel 1"
"1096"	"Throttle Jewel 2"
"1097"	"KO Jewel 1"
"1098"	"Drain Jewel 1"
"1099"	"Drain Jewel 2"
"1100"	"Sonorous Jewel 1"
"1101"	"Artillery Jwl 1"
"1102"	"Resistor Jewel 3"
"1103"	"Nul-Fire Jewel 1"
"1104"	"Nul-Water Jwl 1"
"1105"	"Nul-Thundr Jwl 1"
"1106"	"Nul-Ice Jewel 1"
"1107"	"Nul-Dragon Jwl 1"
"1108"	"Sheath Jewel 1"
"1109"	"Sheath Jewel 2"
"1110"	"Refresh Jewel 1"
"1111"	"Refresh Jewel 2"
"1112"	"Ranger Jewel 1"
"1113"	"SlicePlus Jwl 1"
"1114"	"Paracoat Jewel 1"
"1115"	"Paracoat Jewel 2"
"1116"	"Sleepcoat Jwl 1"
"1117"	"Poisoncoat Jwl 1"
"1118"	"Powercoat Jwl 1"
"1119"	"Powercoat Jwl 2"
"1120"	"Rangecoat Jwl 1"
"1121"	"Draincoat Jwl 1"
"1122"	"Guts Jewel 1"
"1123"	"Guts Jewel 2"
"1124"	"Meateater Jwl 1"
"1125"	"Meatlover Jwl 1"
"1126"	"Honeybee Jewel 1"
"1127"	"Queenbee Jewel 1"
"1128"	"Biology Jewel 1"
"1129"	"Microbio Jewel 1"
"1130"	"Spirit Jewel 1"
"1131"	"Spirit Jewel 2"
"1132"	"Flawless Jewel 1"
"1133"	"Flawless Jewel 2"
"1134"	"Steady Jewel 1"
"1135"	"Steady Jewel 3"
"1136"	"Player Jewel 1"
"1137"	"Morale Jewel 1"
"1138"	"Leader Jewel 1"
"1139"	"Leader Jewel 2"
"1140"	"KO Jewel 2"
"1141"	"Charmer Jewel 1"
"1142"	"BlastPlus Jwl 1"
"1143"	"Blastcoat Jwl 1"
"1144"	"Lynians"
"1145"	"Neopterons Vol 1"
"1146"	"Nerscylla"
"1147"	"Seltas Queen"
"1148"	"Piscine Wyverns Vol1"
"1149"	"Herbivores Vol 1"
"1150"	"Herbivores Vol 2"
"1151"	"Bird Wyverns Vol 1"
"1152"	"Bird Wyverns Vol 2"
"1153"	"Bird Wyverns Vol 3"
"1154"	"Fanged Beasts Vol 1"
"1155"	"Fanged Beasts Vol 2"
"1156"	"Fanged Beasts Vol 3"
"1157"	"Rajang"
"1158"	"Tetsucabra"
"1159"	"Zamtrios"
"1160"	"Snake Wyverns Vol 1"
"1161"	"Brachydios"
"1162"	"Deviljho"
"1163"	"Flying Wyverns Vol 1"
"1164"	"Flying Wyverns Vol 2"
"1165"	"Flying Wyverns Vol 3"
"1166"	"Flying Wyverns Vol 4"
"1167"	"Flying Wyverns Vol 5"
"1168"	"Flying Wyverns Vol 6"
"1169"	"Gore Magala"
"1170"	"Akantor"
"1171"	"Fanged Wyverns Vol 1"
"1172"	"Fanged Wyverns Vol 2"
"1173"	"Kirin"
"1174"	"Oroshi Kirin"
"1175"	"Shagaru Magala"
"1176"	"Dah'ren Mohran"
"1177"	"Kushala Daora"
"1178"	"Teostra"
"1179"	"Dalamadur"
"1180"	"Fatalis"
"1181"	"Honed Jwl 1"
"1182"	"Honed Jwl 2"
"1183"	"Honed Jwl 3"
"1184"	"True Honed Jwl 3"
"1185"	"Calm Jwl 1"
"1186"	"Calm Jwl 2"
"1187"	"Calm Jwl 3"
"1188"	"True Calm Jwl 3"
"1189"	"Hale Jwl 1"
"1190"	"Hale Jwl 2"
"1191"	"Hale Jwl 3"
"1192"	"True Hale Jwl 3"
"1193"	"Ire Jwl 1"
"1194"	"Ire Jwl 2"
"1195"	"Ire Jwl 3"
"1196"	"True Ire Jwl 3"
"1197"	"Nimble Jwl 1"
"1198"	"True Nimble Jwl 1"
"1199"	"Nimble Jwl 2"
"1200"	"Nimble Jwl 3"
"1201"	"True Nimble Jwl 3"
"1202"	"Hardy Jwl 1"
"1203"	"True Hardy Jwl 1"
"1204"	"Hardy Jwl 2"
"1205"	"Hardy Jwl 3"
"1206"	"True Hardy Jwl 3"
"1207"	"Withdrawn Jwl 1"
"1208"	"Withdrawn Jwl 3"
"1209"	"Impactful Jwl 1"
"1210"	"Impactful Jwl 3"
"1211"	"Intense Jwl 2"
"1212"	"Intense Jwl 3"
"1213"	"Aegis Jwl 2"
"1214"	"Aegis Jwl 3"
"1215"	"Tactful Jwl 2"
"1216"	"Tactful Jwl 3"
"1217"	"Broad Jwl 2"
"1218"	"Broad Jwl 3"
"1219"	"Trove Jwl 1"
"1220"	"True Trove Jwl 1"
"1221"	"Trove Jwl 2"
"1222"	"True Trove Jwl 2"
"1223"	"Trove Jwl 3"
"1224"	"Avid Jwl 1"
"1225"	"True Avid Jwl 1"
"1226"	"Avid Jwl 2"
"1227"	"True Avid Jwl 2"
"1228"	"Avid Jwl 3"
"1229"	"Power++ Nectar"
"1230"	"Stamina++ Nectar"
"1231"	"Speed++ Nectar"
"1232"	"Nectar: Fire++"
"1233"	"Nectar: Water++"
"1234"	"Nectar: Thunder++"
"1235"	"Nectar: Ice++"
"1236"	"Nectar: Dragon++"
"1237"	"Wystone Lv1: Brace"
"1238"	"Wystone Lv1: Element"
"1239"	"Wystone Lv1: Force"
"1240"	"Wystone Lv1: Drive"
"1241"	"Piscine Wyverns Vol2"
"1242"	"Herbivores Vol 3"
"1243"	"Carapaceons Vol 1"
"1244"	"Carapaceons Vol 2"
"1245"	"Neopterons Vol 2"
"1246"	"Neopterons Vol 3"
"1247"	"Flying Wyverns Vol 7"
"1248"	"Flying Wyverns Vol 8"
"1249"	"Seregios"
"1250"	"Fanged Beasts Vol 4"
"1251"	"Amphibians"
"1252"	"Snake Wyverns Vol 2"
"1253"	"dummy005"
"1254"	"Ukanlos"
"1255"	"Elder Dragons"
"1256"	"Gogmazios"
"1257"	"Coinperch S"
"1258"	"Coinperch M"
"1259"	"Coinperch L"
"1260"	"Cathangeafish Fry"
"1261"	"Cathangeafish"
"1262"	"Dundorma Tuna"
"1263"	"Temnocerans"
"1264"	"Rathian Shard"
"1265"	"Rathian Cortex"
"1266"	"Rathian Surspike"
"1267"	"Rathian Mantle"
"1268"	"Rath Wingtalon+"
"1269"	"P.Rathian Shard"
"1270"	"P.Rathian Cortex"
"1271"	"G.Rathian Shard"
"1272"	"G.Rathian Cortex"
"1273"	"G.Rath Surspike"
"1274"	"Firegleam Dollop"
"1275"	"Rathalos Shard"
"1276"	"Rathalos Cortex"
"1277"	"Rathalos Lash"
"1278"	"Rathalos Fellwing"
"1279"	"Rathalos Mantle"
"1280"	"A.Rathalos Shard"
"1281"	"A.Rathalos Cortex"
"1282"	"A.Rathalos Lash"
"1283"	"S.Rathalos Shard"
"1284"	"S.Rath Cortex"
"1285"	"S.Rath Lash"
"1286"	"Kut-Ku Shard"
"1287"	"Kut-Ku Cortex"
"1288"	"Kut-Ku Fellwing"
"1289"	"Kut-Ku Luckear"
"1290"	"Blue Kut-Ku Shard"
"1291"	"Blue Kut-Ku Cortex"
"1292"	"Garuga Shard"
"1293"	"Garuga Cortex"
"1294"	"Garuga Silverpelt"
"1295"	"Garuga Fellwing"
"1296"	"Garuga Lash"
"1297"	"Rubbery Finehide"
"1298"	"Gypceros Hardhead"
"1299"	"Rubbery Purple Hide+"
"1300"	"P.Gypceros Fellwing"
"1301"	"Tigrex Shard"
"1302"	"Tigrex Cortex"
"1303"	"Tigrex Hardfang"
"1304"	"Tigrex Hardclaw"
"1305"	"Tigrex Lash"
"1306"	"Tigrex Mantle"
"1307"	"B.Tigrex Shard"
"1308"	"B.Tigrex Cortex"
"1309"	"B.Tigrex Hardfang"
"1310"	"B.Tigrex Hardclaw"
"1311"	"M.Tigrex Shard"
"1312"	"M.Tigrex Cortex"
"1313"	"M.Tigrex Hardfang"
"1314"	"M.Tigrex Trigger"
"1315"	"M.Tigrex Lash"
"1316"	"Dire Blastheart"
"1317"	"Ioprey Piel"
"1318"	"Velocidrome Hardclaw"
"1319"	"Congalala Fur"
"1320"	"Congalala Hardclaw"
"1321"	"Beastly Fang"
"1322"	"E.Congalala Fur"
"1323"	"E.Congalala Hardclaw"
"1324"	"Vibrant Comb"
"1325"	"Rajang Wildpelt"
"1326"	"Rajang Hardhorn"
"1327"	"Rajang Hardfang"
"1328"	"Rajang Hardclaw"
"1329"	"Gold Rajang Pelt+"
"1330"	"Rajang Apoplexy"
"1331"	"Ghoulish Gold Gorer"
"1332"	"Rajang Heart"
"1333"	"Kecha Fur"
"1334"	"Kecha Leadbone"
"1335"	"Kecha Auricle"
"1336"	"Kecha Ripper"
"1337"	"Kecha Lash"
"1338"	"A.Kecha Fur"
"1339"	"A.Kecha Leadbone"
"1340"	"A.Kecha Auricle"
"1341"	"A.Kecha Ripper"
"1342"	"A.Kecha Lash"
"1343"	"Tetsucabra Shard"
"1344"	"Tetsucabra Cortex"
"1345"	"Tetsucabra Gnawrl"
"1346"	"Tetsucabra Hardclaw"
"1347"	"Vicious Visage"
"1348"	"B.Tetsucabra Shard"
"1349"	"B.Tetsucabra Cortex"
"1350"	"B.Tetsucabra Gnawrl"
"1351"	"B.TetsucabraHardclaw"
"1352"	"Zamtrios Shard"
"1353"	"Zamtrios Piel"
"1354"	"Zamtrios Grandfin"
"1355"	"Zamtrios Sawtooth+"
"1356"	"Zamtrios Tailbrand"
"1357"	"T.Zamtrios Shard"
"1358"	"T.Zamtrios Piel"
"1359"	"T.Zamtrios Grandfin"
"1360"	"T.Zamtrios Sawtooth+"
"1361"	"T.Zamtrios Tailbrand"
"1362"	"Najarala Piel"
"1363"	"Najarala Cortex"
"1364"	"Heart-stopping Beak"
"1365"	"Najarala Ampsounder"
"1366"	"T.Najarala Piel"
"1367"	"T.Najarala Cortex"
"1368"	"T.Najarala Splasher"
"1369"	"Dalamadur Steel+"
"1370"	"Dalamadur Glaive+"
"1371"	"Dalamadur Pectus+"
"1372"	"Dalamadur Ripper"
"1373"	"Dalamadur Fanblade+"
"1374"	"Dalam Tail Scale+"
"1375"	"Dalam Tail Shell+"
"1376"	"Skyblade Drgnsphire"
"1377"	"S.Dalamadur Steel+"
"1378"	"S.Dalamadur Glaive+"
"1379"	"S.Dalamadur Gazer"
"1380"	"S.Dalamadur Pectus+"
"1381"	"S.Dalamadur Ripper"
"1382"	"S.Dalam Fanblade+"
"1383"	"S.Dalam Tail Scale+"
"1384"	"S.Dalam Tail Shell+"
"1385"	"Seltas Cortex"
"1386"	"Seltas Razorwing"
"1387"	"Seltas Hardhorn"
"1388"	"D.Seltas Cortex"
"1389"	"D.Seltas Razorwing"
"1390"	"D.Seltas Hardhorn"
"1391"	"S.Queen Cortex"
"1392"	"S.Queen Pectus+"
"1393"	"S.Queen Maw"
"1394"	"S.Queen Hardclaw"
"1395"	"S.Queen Moonlash"
"1396"	"D.S.QueenConcentrate"
"1397"	"D.S.Queen Cortex"
"1398"	"D.S.Queen Pectus+"
"1399"	"D.S.Queen Maw"
"1400"	"D.S.Queen Hardclaw"
"1401"	"D.S.Queen Moonlash"
"1402"	"Nerscylla Cortex"
"1403"	"Nerscylla Scishorn"
"1404"	"Nerscylla Hardclaw"
"1405"	"Nerscylla Surspike"
"1406"	"S.Nerscylla Cortex"
"1407"	"S.Nerscylla Scishorn"
"1408"	"S.Nerscylla Hardclaw"
"1409"	"S.Nerscylla Surspike"
"1410"	"Gore Magala Shard"
"1411"	"Gore Magala Cortex"
"1412"	"Gore Magala Sensor"
"1413"	"Gore Magala Shredder"
"1414"	"Gore Magala Eclipse"
"1415"	"Gore Magala Lash"
"1416"	"Gore Magala Mantle"
"1417"	"Chaos Scale"
"1418"	"Diametrical Horn"
"1419"	"Antinomic Wing"
"1420"	"Contrary Scale"
"1421"	"S.Magala Shard"
"1422"	"S.Magala Cortex"
"1423"	"S.Magala Hardhorn"
"1424"	"S.Magala Purifier"
"1425"	"S.Magala Lightwing"
"1426"	"S.Magala Lash"
"1427"	"S.Magala Mantle"
"1428"	"Daora Cortex"
"1429"	"Daora Hardhorn"
"1430"	"Daora Fellwing"
"1431"	"Daora Lash"
"1432"	"Daora Shard"
"1433"	"Daora Hardclaw"
"1434"	"Steelrust Drakeshell"
"1435"	"Silverpeak Corona"
"1436"	"Steelrust Claw"
"1437"	"Maelstriker Wing"
"1438"	"Steelrust Scale"
"1439"	"Flame Shard"
"1440"	"Teostra Cortex"
"1441"	"Teostra Hardhorn"
"1442"	"Fire Dragon Hardclaw"
"1443"	"Teostra Lash"
"1444"	"Akantor Shard"
"1445"	"Akantor Cortex"
"1446"	"Akantor Greatfang"
"1447"	"Akantor Shredtalon"
"1448"	"Akantor Thickspike"
"1449"	"Akantor Flail"
"1450"	"Conquest Sphere"
"1451"	"Kirin Finehide"
"1452"	"Kirin Azure Peak"
"1453"	"Platinum Mane"
"1454"	"Kirin Thunderlash"
"1455"	"Kirin Ice Peak"
"1456"	"Kirin Frostlash"
"1457"	"Pearl Glosshide"
"1458"	"Khezu Special Cut"
"1459"	"Alluring Glosshide"
"1460"	"Alluring Fellwing"
"1461"	"Basarios Cortex"
"1462"	"Basarios Fellwing"
"1463"	"R.Basarios Cortex"
"1464"	"R.Basarios Fellwing"
"1465"	"Gravios Cortex"
"1466"	"Gravios Crown"
"1467"	"Gravios Fellwing"
"1468"	"Gravios Pallium"
"1469"	"B.Gravios Cortex"
"1470"	"B.Gravios Crown"
"1471"	"B.Gravios Pallium"
"1472"	"Deviljho Shard"
"1473"	"Deviljho Blackpiel"
"1474"	"Deviljho Hardfang"
"1475"	"Deviljho Ripper"
"1476"	"Deviljho Flail"
"1477"	"Deviljho Crook"
"1478"	"Bloodied Ebonhide"
"1479"	"Vile Fang"
"1480"	"Brach Cortex"
"1481"	"Fine Brach Ebonshell"
"1482"	"Brach Crown"
"1483"	"Brach Pounder+"
"1484"	"Brach Lash"
"1485"	"Brach Pallium"
"1486"	"Glowing Slime"
"1487"	"dummy006"
"1488"	"dummy007"
"1489"	"dummy008"
"1490"	"dummy009"
"1491"	"dummy010"
"1492"	"dummy011"
"1493"	"D.Mohran Rockskin"
"1494"	"D.Mohran Heavyscale"
"1495"	"Brutal Biter"
"1496"	"D.Mohran Allbrace"
"1497"	"Earth Dragonsphire"
"1498"	"Lagombi Fur"
"1499"	"Lagombi Auricle"
"1500"	"Zinogre Electrofur+"
"1501"	"Zinogre Cortex"
"1502"	"Zinogre Hardhorn"
"1503"	"Zinogre Hardclaw"
"1504"	"Zinogre D-Shocker"
"1505"	"Zinogre Lash"
"1506"	"Zinogre Skymerald"
"1507"	"S.Zin Dragonlocks"
"1508"	"S.Zinogre Cortex"
"1509"	"S.Zinogre Hardhorn"
"1510"	"S.Zinogre Hardclaw"
"1511"	"S.Zinogre Drakehold"
"1512"	"S.Zinogre Lash"
"1513"	"S.Zinogre Skymerald"
"1514"	"Seregios Slavescale"
"1515"	"Seregios Slavescale+"
"1516"	"Seregios Airblade"
"1517"	"Seregios Airblade+"
"1518"	"Seregios Carver"
"1519"	"Seregios Carver+"
"1520"	"Seregios Impaler"
"1521"	"Seregios Impaler+"
"1522"	"Seregios Breacher"
"1523"	"Seregios Breacher+"
"1524"	"Seregios Scraper"
"1525"	"Seregios Scraper+"
"1526"	"Seregios Dissenter"
"1527"	"Seregios Lens"
"1528"	"Gogmazios Battlement"
"1529"	"Gogmazios Fortress"
"1530"	"Gogmazios Gasher"
"1531"	"Gogmazios Offenders"
"1532"	"Gogmazios Fiberwing"
"1533"	"Gogmazios Briartail"
"1534"	"Wartorn Dragonsphire"
"1535"	"Heavy Drakesbone Oil"
"1536"	"Diablos Cortex"
"1537"	"Diablos Chine"
"1538"	"Twisted Stouthorn"
"1539"	"Diablos Hardhorn"
"1540"	"Diablos Tailcase+"
"1541"	"Blos Medulla"
"1542"	"B.Diablos Cortex"
"1543"	"B.Diablos Chine"
"1544"	"Blackcurl Stouthorn"
"1545"	"Monoblos Carapace"
"1546"	"Monoblos Cortex"
"1547"	"Monoblos Thoracic"
"1548"	"Monoblos Chine"
"1549"	"Scarlet Finehorn"
"1550"	"Scarlet Stouthorn"
"1551"	"Monoblos Heart"
"1552"	"W.Monoblos Cortex"
"1553"	"W.Monoblos Chine"
"1554"	"Argent Stouthorn"
"1555"	"Hermitaur Cortex"
"1556"	"Hermitaur Hardclaw"
"1557"	"Black Pearl"
"1558"	"Fine Black Pearl"
"1559"	"Wyvern Scalp"
"1560"	"Hermitaur Purpshell"
"1561"	"Hermitaur Purpclaw"
"1562"	"Cephalos Shard"
"1563"	"Cephalos Purpscale"
"1564"	"Cephalos Grandfin"
"1565"	"Cephalos Pinkfin"
"1566"	"Cephalos Fang+"
"1567"	"Cephalos Hardfang"
"1568"	"Piscine Liver"
"1569"	"Ukanlos Cortex"
"1570"	"Ukanlos Underscale"
"1571"	"Ukanlos Underscale+"
"1572"	"Ukanlos Tail"
"1573"	"Ukanlos Flail"
"1574"	"Ukanlos Digger"
"1575"	"Ukanlos Digger+"
"1576"	"Ukanlos Shoveljaw+"
"1577"	"Ukanlos Fin+"
"1578"	"Ukanlos Strongfin"
"1579"	"Ukanlos Stone"
"1580"	"Chameleos Finehide"
"1581"	"Chameleos Hardhorn"
"1582"	"Chameleos Fellwing"
"1583"	"Chameleos Lash"
"1584"	"Fatalis Shard"
"1585"	"Fatalis Cortex"
"1586"	"Fatalis Hardhorn"
"1587"	"Fatalis Evil Eye"
"1588"	"Fatalis Fellwing"
"1589"	"Fatalis Flamescale"
"1590"	"Fatalis Flameshell"
"1591"	"Fatalis Flamehorn"
"1592"	"Fatalis Flamewing"
"1593"	"Fatalis Devileye+"
"1594"	"Old Fatalis Shard"
"1595"	"Old Fatalis Cortex"
"1596"	"Old Fatalis Fellwing"
"1597"	"Old Fatalis Hardhorn"
"1598"	"Old Fatalis Souleye"
"1599"	"Deadly Poison Sac"
"1600"	"Conflagrant Sac"
"1601"	"Flood Sac"
"1602"	"Cryo Sac"
"1603"	"Lightning Sac"
"1604"	"Omniplegia Sac"
"1605"	"Coma Sac"
"1606"	"Fey Wyvern Gem"
"1607"	"Paddock Cream"
"1608"	"Lrg ElderDragon Bone"
"1609"	"Lrg Elder Dragon Gem"
"1610"	"Lrg Wyvern Gem"
"1611"	"Monster Slogbone"
"1612"	"Dragonbone Artifact"
"1613"	"Massive Bone"
"1614"	"Prized Pelt"
"1615"	"Rhenoplos Bone"
"1616"	"Avian Stoutbone"
"1617"	"Remobra Finehide"
"1618"	"Meaty Piel"
"1619"	"Crushing Fang"
"1620"	"Sml Hermitaur Shell"
"1621"	"Piercing Claw"
"1622"	"Bnahabra Razorwing"
"1623"	"Konchu Cortex"
"1624"	"Monster Essence"
"1625"	"Carapaceon Brains"
"1626"	"Carapaceon Brains+"
"1627"	"Crab Pearl"
"1628"	"Crab Pearl+"
"1629"	"Emperor Hopper"
"1630"	"Flutterfly"
"1631"	"Elder Bfly Beetle"
"1632"	"Great Elder Hornfly"
"1633"	"Divine Rhino"
"1634"	"Meteor Crystal"
"1635"	"Eltalite Ore"
"1636"	"Meldspar Ore"
"1637"	"Allfire Stone"
"1638"	"Purecrystal"
"1639"	"Cosmicite Ore"
"1640"	"Distorted Fragment"
"1641"	"Ultimate Abrasive"
"1642"	"Sovereign Orb"
"1643"	"Strong Armor Sphere"
"1644"	"Divine Armor Sphere"
"1645"	"Pure Frenzy Crystal"
"1646"	"Zenith Wyvern Gem"
"1647"	"Zenith Wyvern Gem L"
"1648"	"Enduring Charm"
"1649"	"Graven Charm"
"1650"	"Champion's Weapon"
"1651"	"Beshackled Weapon"
"1652"	"Champion's Armor"
"1653"	"Beshackled Armor"
"1654"	"Gloamgrass Bud"
"1655"	"Humble Scrap+"
"1656"	"Handsome Scrap+"
"1657"	"Gargwa Scrap+"
"1658"	"Arzuros Scrap+"
"1659"	"Rhenoplos Scrap+"
"1660"	"Great Jaggi Scrap+"
"1661"	"Bnahabra Scrap+"
"1662"	"Bulldrome Scrap+"
"1663"	"Slagtoth Scrap+"
"1664"	"Volvidon Scrap+"
"1665"	"Cephalos Scrap+"
"1666"	"Konchu Scrap+"
"1667"	"Kecha Wacha Scrap+"
"1668"	"Yian Kut-Ku Scrap+"
"1669"	"Tetsucabra Scrap+"
"1670"	"Barroth Scrap+"
"1671"	"Zamtrios Scrap+"
"1672"	"Lagombi Scrap+"
"1673"	"Uragaan Scrap+"
"1674"	"Rathian Scrap+"
"1675"	"Khezu Scrap+"
"1676"	"Great Wroggi Scrap+"
"1677"	"Congalala Scrap+"
"1678"	"Basarios Scrap+"
"1679"	"Nerscylla Scrap+"
"1680"	"Qurupeco Scrap+"
"1681"	"Nibelsnarf Scrap+"
"1682"	"Tigrex Scrap+"
"1683"	"Great Baggi Scrap+"
"1684"	"Rathalos Scrap+"
"1685"	"Agnaktor Scrap+"
"1686"	"Seltas Queen Scrap+"
"1687"	"Duramboros Scrap+"
"1688"	"Diablos Scrap+"
"1689"	"Rajang Scrap+"
"1690"	"Barioth Scrap+"
"1691"	"Brach Scrap+"
"1692"	"Zinogre Scrap+"
"1693"	"Nargacuga Scrap+"
"1694"	"Najarala Scrap+"
"1695"	"Seregios Scrap+"
"1696"	"Monoblos Scrap+"
"1697"	"Gravios Scrap+"
"1698"	"Yian Garuga Scrap+"
"1699"	"Gypceros Scrap+"
"1700"	"Dalamadur Scrap+"
"1701"	"Amatsu Scrap+"
"1702"	"S.Magala Scrap+"
"1703"	"Gore Magala Scrap+"
"1704"	"Kushala Daora Scrap+"
"1705"	"Ukanlos Scrap+"
"1706"	"Teostra Scrap+"
"1707"	"Deviljho Scrap+"
"1708"	"Kirin Scrap+"
"1709"	"Akantor Scrap+"
"1710"	"Alatreon Scrap+"
"1711"	"Dah'renMohran Scrap+"
"1712"	"Jhen Mohran Scrap+"
"1713"	"Gogmazios Scrap+"
"1714"	"Chameleos Scrap+"
"1715"	"Dust of Life"
"1716"	"Black Belt Coin G"
"1717"	"Expert Coin G"
"1718"	"Legend Coin G"
"1719"	"VE Cosmic Ticket"
"1720"	"Conqueror's Seal"
"1721"	"Conqueror's Seal G"
"1722"	"Caravan Ticket G"
"1723"	"Immenseness Ticket"
"1724"	"Immenseness Ticket S"
"1725"	"Proto Wystone: Drive"
"1726"	"Tropical Berry"
"1727"	"Portable Steam Bomb"
"1728"	"Disposable Earplugs"
"1729"	"Garrison Bllsta Ammo"
"1730"	"Dense Marcoal"
"1731"	"EZ Farcaster"
"1732"	"EZ Lifepowder"
"1733"	"EZ Dust of Life"
"1734"	"Meteor Chunk"
"1735"	"Frozen Berry"
"1736"	"Poogie Pearl"
"1737"	"Brach Warhead"
"1738"	"Brach Obliterator"
"1739"	"Immortal Reactor"
"1740"	"Flashpoint Slime"
"1741"	"G.Plesioth Shard"
"1742"	"Fine G.Plesioth Fin"
"1743"	"Gigginox Scrap+"
"1744"	"Royal Ludroth Scrap+"
"1745"	"Plesioth Scrap+"
"1746"	"Lagiacrus Scrap+"
"1747"	"Hermitaur Scrap+"
"1748"	"S.Barioth Sharpclaw"
"1749"	"J.Barroth Thoracic"
"1750"	"Qurupeco Shard"
"1751"	"Lustrous Feather"
"1752"	"C.Qurupeco Fellwing"
"1753"	"Gobul Piel"
"1754"	"Gobul Surspike"
"1755"	"Vespoid Razorwing"
"1756"	"Arzuros Cortex"
"1757"	"Nibelsnarf Cortex"
"1758"	"Dazzling Fluid"
"1759"	"Ceanataur Terraclaw"
"1760"	"Barioth Thickfur"
"1761"	"Heavy Amber Tusk"
"1762"	"Barroth Cortex"
"1763"	"Barroth Crown"
"1764"	"S.Barioth Hardclaw"
"1765"	"Volvidon Cortex"
"1766"	"Agnaktor Cortex"
"1767"	"Fine Agnaktor Fin"
"1768"	"J.Barroth Cortex"
"1769"	"Duramboros Cortex"
"1770"	"Duramboros Hardhorn"
"1771"	"Durambolite"
"1772"	"Uragaan Cortex"
"1773"	"Uragaan Pallium"
"1774"	"Nargacuga Blackfur+"
"1775"	"Nargacuga Cutwing+"
"1776"	"Nargacuga Mantle"
"1777"	"Lagiacrus Shard"
"1778"	"Deathly Shocker"
"1779"	"Lagiacrus Mantle"
"1780"	"I.Lagi D-Shocker"
"1781"	"I.Lagi Hardhorn"
"1782"	"Lavasioth Shard"
"1783"	"Fine Lavasioth Fin"
"1784"	"S.Uragaan Scute+"
"1785"	"Ceadeus Scale"
"1786"	"Ceadeus Strongfur"
"1787"	"Deep Dragongem"
"1788"	"Tsukami Fillet"
"1789"	"Ancient Dragonmoss"
"1790"	"Ancient Dragonwood"
"1791"	"Mohran Rockskin"
"1792"	"Mohran Allbrace"
"1793"	"Deific Fang"
"1794"	"Lunastra Cortex"
"1795"	"Lunastra Tail"
"1796"	"Lao-Shan Azure Claw"
"1797"	"Miralis Scale"
"1798"	"Miralis Smelter"
"1799"	"Dire Dragongem"
"1800"	"A.Lagi D-Shocker"
"1801"	"A.Lagi Dynamo"
"1802"	"L.Nargacuga Razor"
"1803"	"Cloudy Moonshard"
"1804"	"Lao-Shan Cortex"
"1805"	"Lao-Shan Shard"
"1806"	"Lao-Shan Mantle"
"1807"	"Amatsu Carapace"
"1808"	"Amatsu Webbing"
"1809"	"Heavenly Dragongem"
"1810"	"Alatreon Mantle"
"1811"	"Alatreon Riptalon"
"1812"	"Azure Dragonsphire"
"1813"	"Wystone Lv2: Brace"
"1814"	"Wystone Lv2: Element"
"1815"	"Wystone Lv2: Force"
"1816"	"Wystone Lv2: Drive"
"1817"	"Fardrake's Eye"
"1818"	"CritElementJwl 1"
"1819"	"CritElementJwl 2"
"1820"	"Crit Status Jwl 1"
"1821"	"Crit Status Jwl 2"
"1822"	"Precise Jewel 1"
"1823"	"Precise Jewel 2"
"1824"	"Fungiform Jewel 1"
"1825"	"Satiated Jewel 1"
"1826"	"Staunch Jewel 1"
"1827"	"Clean Jewel 1"
"1828"	"Rodeo Jewel 1"
"1829"	"Sane Jewel 1"
"1830"	"Sane Jewel 3"
"1831"	"Artisan Jewel 3"
"1832"	"Defense Jewel 3"
"1833"	"TrueCatalystJwl 3"
"1834"	"Cutting Jwl 1"
"1835"	"Cutting Jwl 2"
"1836"	"Cutting Jwl 3"
"1837"	"TrueCuttingJwl 3"
"1838"	"Arcane Jwl 1"
"1839"	"Arcane Jwl 2"
"1840"	"Arcane Jwl 3"
"1841"	"True Arcane Jwl 3"
"1842"	"Brawn Jwl 1"
"1843"	"Brawn Jwl 2"
"1844"	"Brawn Jwl 3"
"1845"	"True Brawn Jwl 3"
"1846"	"Supplicant Jwl 1"
"1847"	"Supplicant Jwl 2"
"1848"	"Supplicant Jwl 3"
"1849"	"Furtive Jwl 1"
"1850"	"Furtive Jwl 2"
"1851"	"Furtive Jwl 3"
"1852"	"TrueFurtiveJwl 3"
"1853"	"True Honed Jwl 2"
"1854"	"UltimateHonedJwl3"
"1855"	"True Calm Jwl 2"
"1856"	"UltimateCalmJwl 3"
"1857"	"True Hale Jwl 2"
"1858"	"UltimateHaleJwl 3"
"1859"	"True Ire Jwl 2"
"1860"	"UltimateIreJwl 3"
"1861"	"Withdrawn Jwl 2"
"1862"	"TrueWithdrawnJwl3"
"1863"	"Impactful Jwl 2"
"1864"	"TrueImpactfulJwl3"
"1865"	"TrueIntenseJwl 2"
"1866"	"TrueIntenseJwl 3"
"1867"	"True Aegis Jwl 2"
"1868"	"True Aegis Jwl 3"
"1869"	"TrueTactfulJwl 2"
"1870"	"TrueTactfulJwl 3"
"1871"	"True Broad Jwl 2"
"1872"	"True Broad Jwl 3"
"1873"	"dummy012"
"1874"	"Laden Jewel 3"
"1875"	"Daimyo Coin"
"1876"	"Argosy Ticket"
"1877"	"Yukumo Ticket"
"1878"	"Felcote Ticket"
"1879"	"Shakalaka Ticket"
"1880"	"Instructor's Ticket"
"1881"	"Guild Clerk Ticket"
"1882"	"Jumbo Ticket"
"1883"	"Hunter's Ticket"
"1884"	"Deviljho Ticket"
"1885"	"Amatsu Ticket"
"1886"	"Crimson Fatalis Ticket"
"1887"	"Wonderful Pass"
"1888"	"Special Pass"
"1889"	"dummy"
"1890"	"dummy"
"1891"	"dummy"
"1892"	"dummy"
"1893"	"Clockwork Blueprint"
"1894"	"dummy"
"1895"	"Fan Club Newsletter"
"1896"	"Fan Club Cheer"
"1897"	"E-Tank"
"1898"	"Vintage Meat Ticket"
"1899"	"Fake Catalog"
"1900"	"Recruitment Poster"
"1901"	"Hero's Seal"
"1902"	"Design Pattern"
"1903"	"Weapon Data"
"1904"	"Defense Module "
"1905"	"The Tale of Poogie"
"1906"	"Goddess's Grace"
"1907"	"Gold Ring"
"1908"	"Festival Drum Music"
"1909"	"dummy"
"1910"	"dummy"
"1911"	"dummy"
"1912"	"dummy"
"1913"	"dummy"
"1914"	"dummy"
"1915"	"dummy"
"1916"	"dummy"
"1917"	"Proto Dense Marcoal"
"1918"	"Flying Coin G"
"1919"	"Brute Coin G"
"1920"	"Vermin Coin G"
"1921"	"Toad Coin G"
"1922"	"Snake Coin G"
"1923"	"Ace Ticket"
"1924"	"10,000 Zenny"
"1925"	"1,000 Caravan Points"
"1926"	"Jumping Jewel 2"
"1927"	"Live Bombfish"
"1928"	"Blue Marlin"
"1929"	"Horse Mackerel"
"1930"	"Pop-eyed Goldfish"
"1931"	"Giant Stag"
"1932"	"Darner Dragonfly"
"1933"	"Common Butterfly"
"1934"	"Flea"
"1935"	"EZ Marcoal"
"1936"	"(None)"
"""

LOCK = threading.Lock()
ST = {"raw": None, "plain": None, "path": None, "fname": ""}
NAMES = []
META = {"ic": [], "gr": []}


def parse_names(text):
    names = []
    for line in text.splitlines():
        p = line.split("\t")
        if len(p) == 2:
            n = p[1]
            if len(n) >= 2 and n[0] == n[-1] == '"':
                n = n[1:-1]
            names.append(n)
    return names


# ------------------------------------------------------------ Ikon ---------
# Ikon dibaca dari folder icon/ di sebelah script ini.
# Nama file: MH4U-<Kategori>_Icon_<Warna>.png  (mis. MH4U-Bone_Icon_Gray.png)
# Item yang kategorinya belum punya ikon di folder akan tampil sebagai lencana
# teks kecil. Kalau nanti kamu menambah ikon baru (Claw, Scale, Herb, Ore, dst.)
# cukup taruh di folder icon/ lalu restart server; otomatis terpakai.
ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon")
ICON_RE = re.compile(r"^MH4U-([A-Za-z0-9_]+)_Icon_([A-Za-z0-9_]+)\.png$")
COLORS = ["Blue", "Brown", "Emerald", "Gray", "Green", "Light_Blue", "Light_Green",
          "Moss", "Orange", "Pink", "Purple", "Red", "White", "Yellow"]
AVAIL = {}  # kategori -> set warna yang tersedia

# key, label, singkatan lencana, warna lencana
GROUPS = [
    ("potion", "Ramuan", "POT", "#4fae6a"),
    ("ammo", "Amunisi", "AMO", "#c9a14a"),
    ("bomb", "Bom & Jebakan", "BOM", "#d9694a"),
    ("tool", "Alat", "ALT", "#8a9bb0"),
    ("food", "Makanan", "FOD", "#c77a5a"),
    ("plant", "Herba & Jamur", "HRB", "#7fb347"),
    ("ore", "Bijih & Permata", "ORE", "#6fb3d9"),
    ("critter", "Ikan & Serangga", "BUG", "#a3c94f"),
    ("bone", "Tulang", "BON", "#d8d2c0"),
    ("sphere", "Armor Sphere", "SPH", "#8fa6e0"),
    ("charm", "Charm", "CHM", "#5fc4a8"),
    ("jewel", "Jewel", "JWL", "#b88fd9"),
    ("part", "Material Monster", "PRT", "#d6a05a"),
    ("ticket", "Tiket, Koin & Harta", "TKT", "#e0c04a"),
    ("book", "Buku & Data", "BUK", "#b9774f"),
    ("misc", "Lainnya", "???", "#9a958b"),
]


def scan_icons():
    AVAIL.clear()
    try:
        files = os.listdir(ICON_DIR)
    except OSError:
        return
    for f in files:
        m = ICON_RE.match(f)
        if m:
            AVAIL.setdefault(m.group(1), set()).add(m.group(2))


def hc(s):
    """Warna stabil dari teks: nama yang sama selalu dapat warna yang sama."""
    return COLORS[zlib.crc32(s.encode("utf-8")) % len(COLORS)]


def pick(n, table, default):
    for pat, val in table:
        if re.search(pat, n):
            return val
    return default


POTION_COL = {8: "Green", 9: "Blue", 10: "Orange", 11: "Red", 12: "Light_Green", 13: "Emerald",
              14: "Yellow", 15: "Brown", 16: "Red", 17: "Pink", 18: "Purple", 19: "Moss",
              20: "Emerald", 21: "Orange", 22: "Light_Blue", 23: "Orange", 24: "White",
              25: "Gray", 26: "Purple", 27: "Moss", 28: "Red", 29: "Pink", 30: "Yellow", 31: "Yellow"}
AMMO_COL = [("normal", "White"), ("pierce", "White"), ("pellet", "White"), ("crag", "White"),
            ("clust", "White"), ("flaming", "Red"), ("water", "Light_Blue"), ("thunder", "Yellow"),
            ("freeze", "Blue"), ("dragon", "Red"), ("poison", "Purple"), ("para", "Yellow"),
            ("sleep", "Light_Blue"), ("exhaust", "Blue"), ("slicing", "White"), ("wyvernfire", "Red"),
            ("blast", "Orange"), ("recover", "Green"), ("demon", "Red"), ("armor", "Orange"),
            ("paint", "Pink"), ("tranq", "Red")]
COAT_COL = [("empty", "White"), ("no coating", "Gray"), ("power", "Red"), (r"c\.range", "White"),
            ("poison", "Purple"), ("para", "Yellow"), ("sleep", "Light_Blue"), ("exhaust", "Blue"),
            ("blast", "Orange"), ("paint", "Pink")]
NECTAR_COL = [("fire", "Red"), ("water", "Blue"), ("thunder", "Yellow"), ("ice", "Light_Blue"),
              ("dragon", "Purple"), ("power", "Orange"), ("stam", "Green"), ("spe*d", "Pink")]
BAIT_COL = {155: "Brown", 156: "Yellow", 157: "Green", 158: "Pink", 159: "Orange", 160: "Yellow"}
SPECIAL = {  # id: (group, kategori ikon, warna)
    54: ("tool", "Boomerang", "White"), 55: ("tool", "Knife", "Gray"),
    56: ("tool", "Knife", "Purple"), 57: ("tool", "Knife", "Light_Blue"),
    58: ("tool", "Knife", "Yellow"), 59: ("tool", "Knife", "Green"),
    60: ("bomb", "Bomb", "Gray"), 61: ("bomb", "Ball", "Pink"), 62: ("bomb", "Ball", "Yellow"),
    63: ("bomb", "Ball", "Light_Blue"), 64: ("bomb", "Dung", "Brown"), 65: ("bomb", "Ball", "Brown"),
    66: ("bomb", "Smoke_Bomb", "Gray"), 67: ("bomb", "Smoke_Bomb", "Purple"), 68: ("bomb", "Ball", "Blue"),
    69: ("bomb", "Ball", "Emerald"), 70: ("tool", "Spiderweb", "White"), 71: ("tool", "Bugnet", "Brown"),
    72: ("bomb", "Trap", "Gray"), 73: ("bomb", "Trap", "Brown"), 74: ("bomb", "Trap", "Yellow"),
    75: ("bomb", "Barrel", "Brown"), 76: ("bomb", "Barrel", "Orange"),
    77: ("bomb", "Bomb", "Orange"), 78: ("bomb", "Bomb", "Red"), 79: ("bomb", "Bomb", "Purple"),
    80: ("bomb", "Bomb", "Pink"), 81: ("bomb", "Bomb", "Green"), 82: ("bomb", "Bomb", "Moss"),
    83: ("bomb", "Bomb", "Emerald"), 85: ("plant", "Seed", "Brown"), 86: ("tool", "Husk", "Brown"),
    27: ("potion", "Medicine", "Green"), 161: ("tool", "Pickaxe", "Brown"), 162: ("tool", "Pickaxe", "Gray"),
    163: ("tool", "Pickaxe", "Yellow"), 167: ("tool", "Spit", "Brown"), 168: ("tool", "Whetstone", "Gray"),
    171: ("tool", "Horn", "Brown"), 172: ("tool", "Horn", "Green"), 173: ("tool", "Horn", "Light_Green"),
    174: ("tool", "Horn", "Red"), 175: ("tool", "Horn", "Gray"), 248: ("critter", "Husk", "Gray"),
    768: ("book", "Map", "Brown"), 772: ("tool", "Spit", "Gray"), 773: ("tool", "Whetstone", "White"),
    785: ("tool", "Torch", "Orange"), 842: ("ticket", "Meal_Ticket", "Yellow"),
    843: ("ticket", "Meal_Ticket", "Purple"), 1898: ("ticket", "Meal_Ticket", "Red"),
    1237: ("ore", "Wystone_Brace", "Blue"), 1238: ("ore", "Wystone_Element", "Red"),
    1239: ("ore", "Wystone_Force", "Orange"), 1240: ("ore", "Wystone_Drive", "Green"),
    1813: ("ore", "Wystone_Brace", "Purple"), 1814: ("ore", "Wystone_Element", "Pink"),
    1815: ("ore", "Wystone_Force", "Yellow"), 1816: ("ore", "Wystone_Drive", "Emerald"),
    1725: ("ore", "Wystone_Drive", "Gray"),
    164: ("tool", "Bugnet", "Brown"), 165: ("tool", "Bugnet", "Green"), 166: ("tool", "Bugnet", "Yellow"),
    169: ("tool", "Binoculars", "Gray"), 176: ("potion", "Bottle", "Yellow"),
    32: ("bomb", "Sac", "Gray"), 33: ("potion", "Ball", "Light_Green"), 34: ("potion", "Medicine", "Light_Green"),
    170: ("tool", "Question_Mark", "Gray"),
    35: ("potion", "Bottle", "Gray"),
}
CHARM_COL = {36: "Gray", 37: "Emerald", 38: "Brown", 39: "Blue", 40: "Brown", 41: "Blue",
             42: "Gray", 43: "Emerald", 1648: "Emerald", 1649: "Blue"}
SPHERE_COL = {224: "Brown", 225: "Gray", 226: "Green", 227: "Blue", 228: "Purple", 229: "Orange",
              230: "Red", 1643: "Pink", 1644: "Yellow"}
BUG_COL = {"insect husk": "Brown", "stinkhopper": "Green", "snakebee larva": "Yellow",
           "godbug": "Light_Green", "bitterbug": "Moss", "flashbug": "Light_Blue",
           "thunderbug": "Yellow", "glueglopper": "Brown", "killer beetle": "Red",
           "hercudrome": "Orange", "fulgurbug": "Light_Blue", "rare scarab": "Pink",
           "great hornfly": "Blue", "butterfly beetle": "Purple", "royal rhino": "Yellow"}
# Monster Bone S dan M sengaja kembar (sama seperti di game).
BONE_COL = {"monster bone s": "Gray", "monster bone m": "Gray", "monster bone l": "White",
            "mystery bone": "Yellow", "unknown skull": "Orange", "monster bone+": "Green",
            "monster keenbone": "Light_Green", "monster hardbone": "Emerald",
            "elder dragon bone": "Red", "avian finebone": "Brown", "brute bone": "Moss",
            "jumbo bone": "Blue", "stoutbone": "Purple", "dragonbone relic": "Pink"}
NAME_SPECIAL = {
    "first-aid med": ("potion", "Medicine", "White"), "first-aid med+": ("potion", "Medicine", "Light_Green"),
    "map": ("book", "Map", "Brown"), "torch": ("tool", "Torch", "Orange"),
    "anti-dragon bomb": ("bomb", "Bomb", "Purple"), "portable steam bomb": ("bomb", "Bomb", "Light_Blue"),
    "secret stash": ("ticket", "Box", "Brown"), "dragon treasure": ("ticket", "Box", "Orange"),
    "old dragon treasure": ("ticket", "Box", "Brown"),
    "ballista ammo": ("ammo", "Ammo", "Gray"), "ballista binder": ("ammo", "Ammo", "Brown"),
    "one-shot binder": ("ammo", "Ammo", "Pink"), "cannon ammo": ("ammo", "Ammo", "Orange"),
    "garrison bllsta ammo": ("ammo", "Ammo", "Gray"),
    "dust of life": ("potion", "Medicine", "Green"),
    "disposable earplugs": ("tool", "Husk", "Gray"),
}
BOOK_KIND = [("flying wyvern", "Red"), ("fanged beast", "Orange"), ("bird wyvern", "Green"),
             ("herbivore", "Light_Green"), ("neopteron", "Yellow"), ("carapaceon", "Brown"),
             ("piscine", "Blue"), ("snake wyvern", "Moss"), ("fanged wyvern", "Purple"),
             ("amphibian", "Emerald"), ("elder dragons", "Pink"), ("lynian", "Gray"),
             ("temnoceran", "Brown")]
PART_CATS = [(r"webbing", ["Webbing"]), (r"pallium", ["Pallium"]), (r"scale|piel", ["Scale"]),
             (r"hide|pelt|fur|skin|mane|plastron", ["Hide"]),
             (r"shell|carapace|plate|scalp|thoracic|pectus|chelicera|cortex", ["Shell"]),
             (r"claw|talon|pincer|fang|tusk|nail", ["Claw"]), (r"horn|beak", ["Horn"])]


def plant_cat(n):
    if re.search(r"mushroom|shroom|toadstool", n):
        return "Mushroom"
    if re.search(r"berry|seed|nut\b|pumpkin|acorn", n):
        return "Seed"
    return "Herb"


def classify_base(i, names, byname):
    """Kembalikan (group, [kategori ikon], warna) untuk item ID i."""
    name = names[i] if i < len(names) else ""
    n = name.lower().strip()
    if i == 0 or not n or n.startswith("dummy") or n in ("invalid message", "(none)", "--------"):
        return "misc", [], None
    if n.startswith("ez ") and i > 700:  # EZ-versi kembar dengan item aslinya
        b = byname.get(n[3:])
        if b is not None and b != i:
            return classify(b, names, byname)
    if i in SPECIAL:
        g, c, col = SPECIAL[i]
        return g, [c] if c else [], col
    if i in CHARM_COL:
        return "charm", ["Charm"], CHARM_COL[i]
    if i in SPHERE_COL:
        return "sphere", ["Armor_Sphere"], SPHERE_COL[i]
    if 1 <= i <= 5:
        return "book", ["Book"], "Brown"
    if i in (6, 7):
        return "book", ["Book"], "Blue"
    if 8 <= i <= 31:
        return "potion", ["Bottle"], POTION_COL.get(i, "Green")
    if 44 <= i <= 53:
        return "food", ["Meat"], hc(n)
    if 84 <= i <= 85:
        return ("plant", [], None) if i == 85 else ("misc", [], None)
    if 87 <= i <= 123:
        return "ammo", ["Ammo"], pick(n, AMMO_COL, "Gray")
    if 124 <= i <= 133:
        return "potion", ["Coating"], pick(n, COAT_COL, "Gray")
    if 134 <= i <= 154 or 1229 <= i <= 1236:
        return "potion", ["Nectar"], pick(n, NECTAR_COL, "Gray")
    if 155 <= i <= 160:
        return "tool", ["Bait"], BAIT_COL[i]
    if 161 <= i <= 175:
        return "tool", [], None
    if 177 <= i <= 205:
        return "plant", [plant_cat(n)], hc(n)
    if 206 <= i <= 223:
        return "ore", ["Chrystal" if "crystal" in n else "Ore"], hc(n)
    if 231 <= i <= 245:
        return "critter", ["Fish"], hc(n)
    if i in (246, 247, 249):
        return "ticket", ["Ticket"], {246: "Brown", 247: "Gray", 249: "Yellow"}[i]
    if 248 <= i <= 262:
        return "critter", ["Bug"], BUG_COL.get(n, hc(n))
    if 263 <= i <= 276:
        return "bone", ["Bone"], BONE_COL.get(n, hc(n))
    # ---- sisanya berdasarkan nama ----
    if n in NAME_SPECIAL:
        g, c, col = NAME_SPECIAL[n]
        return g, [c] if c else [], col
    if "bone" in n or "skull" in n:
        return "bone", ["Bone"], BONE_COL.get(n, hc(n))
    if re.search(r"jwl|jewel", n):
        return "jewel", ["Decoration"], hc(re.sub(r"[\d\s]+$", "", n))
    if 1144 <= i <= 1180 or 1241 <= i <= 1256 or i == 1263 or re.search(
            r"newsletter|tale of|festival drum|design pattern|weapon data|blueprint|catalog|recruitment", n):
        return "book", ["Book"], pick(n, BOOK_KIND, hc(n))
    if re.search(r"keepsake|memento", n):
        return "charm", ["Charm"], "Brown" if "amulet" in n else "Gray"
    if re.search(r"coin\b|zenny|caravan points", n):
        return "ticket", ["Coin"], hc(re.sub(r"\s+g$", "", n))
    if re.search(r"vintage meat ticket|voucher", n):
        return "ticket", ["Meal_Ticket"], hc(n)
    if re.search(r"battered|seasoned|venerable|champion's|beshackled", n):
        return "ticket", ["Box"], hc(n)
    if re.search(r"ticket|\bpass\b|voucher|badge|certificate|invite|commendation|accolade|seal\b|"
                 r"stamp|cheer\b|e-tank|battered|seasoned|venerable|champion's|beshackled", n):
        col = pick(n, [("bronze", "Brown"), ("silver", "Gray"), ("gold", "Yellow"),
                       ("platinum", "White"), ("deluxe", "Purple"), ("cosmic", "Pink"),
                       (r"^ve ", "Blue")], None) or hc(re.sub(r"\s+g$", "", n))
        return "ticket", ["Ticket"], col
    if re.search(r"scrap", n):
        return "part", ["Scrap"], hc(re.sub(r"\+$", "", n))
    if re.search(r"orb\b|sphere", n):
        return "ore", ["Ball"], "Pink" if "sovereign" in n else "Purple"
    if re.search(r"fish|tuna|arowana|wanchovy|cutthroat|marlin|mackerel|coinperch|cathangeafish", n):
        return "critter", ["Fish"], hc(n)
    if re.search(r"beetle|hornfly|hopper|flutterfly|butterfly|rhino|\bstag\b|dragonfly|\bflea\b|"
                 r"dracophage bug|scarab", n):
        return "critter", ["Bug"], BUG_COL.get(n, hc(n))
    if re.search(r"\begg\b", n):
        return "part", ["Egg"], hc(n)
    if re.search(r"\bsac\b", n):
        return "part", ["Sac"], hc(n)
    if re.search(r"meat|steak|jerky|ration|fillet", n):
        return "food", ["Meat"], hc(n)
    if re.search(r"herb|mushroom|shroom|berry|toadstool|\bseed\b|nut\b|pumpkin|\bivy\b|felvine|"
                 r"cathangea|gloamgrass|pepper|sap plant|dragonwood|dragonmoss|mistentoe|acorn", n):
        return "plant", [plant_cat(n)], hc(n)
    m = re.search(r"wystone lv\d: (brace|element|force|drive)", n)
    if m:
        return "ore", ["Wystone_" + m.group(1).capitalize()], hc(n)
    if re.search(r"rustshard|ancient shard|rusted fragment|ancient fragment|distorted fragment", n):
        return "ore", ["Rustshard"], hc(n)
    if re.search(r"gem\b|ruby|jasper|sapphire|pearl|dragonsphire|drgnsphire", n):
        return "ore", ["Decoration"], hc(n)
    if re.search(r"crystal", n):
        return "ore", ["Chrystal"], hc(n)
    if re.search(r"\bore\b|stone|marcoal|\bcoal\b|abrasive|spall|fragment|durambolite|meldspar|eltalite", n):
        return "ore", ["Ore"], hc(n)
    if re.search(r"dust of life|lifepowder|gunpowder|powder\b", n) and "teostra" not in n:
        return "potion", [], None
    if re.search(r"dung|guano", n):
        return "bomb", ["Dung"], hc(n)
    if re.search(r"earplug|net\b", n):
        return "tool", [], None
    cats = pick(n, PART_CATS, ["Monster_Part"])
    return "part", cats, hc(n.split()[0] if n.split() else n)


WIKI_ICON = {  # nama item (huruf kecil) -> (kategori ikon, warna); sumber: monsterhunterwiki.org MH4U
    'accolade': ('Ticket', 'Yellow'), 'accolade g': ('Ticket', 'Yellow'), 'ace hunter coin': ('Coin', 'Yellow'),
    'ace ticket': ('Ticket', 'Blue'), 'adamant pill': ('Sac', 'Orange'), 'adamant seed': ('Seed', 'Orange'),
    'ancient potion': ('Sac', 'Red'), 'anti-dragon bomb': ('Bomb', 'Yellow'), 'antidote': ('Medicine', 'Blue'),
    'antidote herb': ('Herb', 'Blue'), 'antidote horn': ('Horn', 'Blue'), 'argosy ticket': ('Ticket', 'Blue'),
    'armor horn': ('Horn', 'Orange'), 'armor stone': ('Ore', 'White'), 'armorcharm': ('Sac', 'Orange'),
    'armorskin': ('Medicine', 'Orange'), 'armortalon': ('Claw', 'Orange'), 'auristone chunk': ('Ore', 'Yellow'),
    'ballista ammo': ('Ammo', 'Gray'), 'ballista binder': ('Ammo', 'Yellow'), 'barrel bomb l': ('Bomb', 'Red'),
    'barrel bomb l+': ('Bomb', 'Red'), 'barrel bomb s': ('Bomb', 'Yellow'), 'barrel bomb s+': ('Bomb', 'Yellow'),
    'bbq spit': ('Spit', 'Red'), 'beast coin': ('Coin', 'Blue'), 'beast tear': ('Monster_Part', 'Light_Blue'),
    'binoculars': ('Binoculars', 'White'), 'bird coin': ('Coin', 'Pink'), 'bitterbug': ('Bug', 'Blue'),
    'black belt coin g': ('Coin', 'Gray'), 'bloodstone': ('Ore', 'Red'), 'blue marlin': ('Fish', 'Light_Blue'),
    'bomb casing': ('Ball', 'White'), 'bone husk': ('Husk', 'White'), 'book of combos 1': ('Book', 'Gray'),
    'book of combos 2': ('Book', 'Gray'), 'book of combos 3': ('Book', 'White'),
    'book of combos 4': ('Book', 'White'), 'book of combos 5': ('Book', 'Yellow'),
    'boomerang': ('Boomerang', 'Yellow'), 'bounce bomb': ('Bomb', 'Blue'), 'bounce bomb+': ('Bomb', 'Blue'),
    'bronze guild ticket': ('Ticket', 'Brown'), 'brute coin': ('Coin', 'Light_Green'),
    'brute coin g': ('Coin', 'Light_Green'), 'bug net': ('Bugnet', 'White'), 'burnt meat': ('Meat', 'Gray'),
    'burst bait': ('Bait', 'Red'), 'cannon ammo': ('Ammo', 'Gray'), 'carapaceon brains': ('Monster_Part', 'Orange'),
    'carapaceon brains+': ('Monster_Part', 'Orange'), 'caravan ticket': ('Ticket', 'Red'),
    'caravan ticket g': ('Ticket', 'Red'), 'catalyst': ('Sac', 'Gray'), 'chilled meat': ('Meat', 'Light_Blue'),
    'choice mushroom': ('Mushroom', 'White'), 'cleanser': ('Medicine', 'Light_Blue'), 'coal': ('Ore', 'Gray'),
    'commendation': ('Ticket', 'Orange'), 'commendation g': ('Ticket', 'Orange'),
    'common butterfly': ('Bug', 'White'), "conqueror's seal": ('Ticket', 'Red'),
    "conqueror's seal g": ('Ticket', 'Red'), 'cool drink': ('Medicine', 'White'),
    'crab pearl': ('Monster_Part', 'Gray'), 'crab pearl+': ('Monster_Part', 'Gray'),
    'crystal bone': ('Bone', 'Purple'), 'daimyo coin': ('Coin', 'Red'), 'darner dragonfly': ('Bug', 'Green'),
    'dash juice': ('Medicine', 'Yellow'), 'decayed scale': ('Scale', 'Gray'), 'demon horn': ('Horn', 'Red'),
    'demondrug': ('Medicine', 'Red'), 'dense marcoal': ('Coin', 'Yellow'), 'deodorant': ('Smoke_Bomb', 'Light_Blue'),
    'divine rhino': ('Bug', 'Light_Blue'), 'dragon treasure': ('Monster_Part', 'Purple'),
    'drone substance': ('Medicine', 'Yellow'), 'drugged meat': ('Meat', 'Light_Blue'), 'dung': ('Dung', 'Yellow'),
    'dung bomb': ('Dung', 'Brown'), 'dust of life': ('Sac', 'Emerald'), 'empty phial': ('Bottle', 'Gray'),
    'energy drink': ('Medicine', 'Yellow'), 'expert coin g': ('Coin', 'Yellow'), 'ez barrel bomb l': ('Bomb', 'Blue'),
    'ez dust of life': ('Sac', 'Emerald'), 'ez farcaster': ('Smoke_Bomb', 'Green'), 'ez flash bomb': ('Ball', 'Blue'),
    'ez lifepowder': ('Sac', 'White'), 'ez marcoal': ('Coin', 'Yellow'), 'ez max potion': ('Sac', 'Blue'),
    'ez pitfall trap': ('Trap', 'Blue'), 'ez shock trap': ('Trap', 'Purple'), 'ez sonic bomb': ('Ball', 'Green'),
    'fang coin': ('Coin', 'White'), 'farcaster': ('Smoke_Bomb', 'Green'), "fardrake's eye": ('Ore', 'Red'),
    'felvine bomb': ('Bomb', 'Pink'), 'field horn': ('Horn', 'Yellow'), 'fine stomach': ('Sac', 'Yellow'),
    'first-aid med': ('Medicine', 'Green'), 'first-aid med+': ('Medicine', 'Green'), 'flash bomb': ('Ball', 'Yellow'),
    'flea': ('Bug', 'Gray'), 'flying coin': ('Coin', 'Red'), 'flying coin g': ('Coin', 'Red'),
    'fossilized bone': ('Bone', 'White'), 'frog': ('Bait', 'Blue'), 'frozen berry': ('Seed', 'Pink'),
    'garbage': ('Question_Mark', 'Gray'), 'gargwa egg': ('Egg', 'Light_Blue'), 'gargwa guano': ('Dung', 'Yellow'),
    'garrison bllsta ammo': ('Ammo', 'Yellow'), 'gastronome tuna': ('Fish', 'Orange'), 'giant stag': ('Bug', 'Brown'),
    'glutton tuna': ('Fish', 'Orange'), 'gold gargwa egg': ('Egg', 'Yellow'),
    'gold guild ticket': ('Ticket', 'Yellow'), 'goldenfish': ('Fish', 'Yellow'),
    'goldenfish bait': ('Bait', 'Yellow'), 'gourmet voucher': ('Ticket', 'Yellow'), 'gunpowder': ('Sac', 'Red'),
    'health horn': ('Horn', 'Green'), 'herb': ('Herb', 'Green'), 'herbal medicine': ('Sac', 'White'),
    'herbivore egg': ('Egg', 'Orange'), 'horse mackerel': ('Fish', 'Gray'), 'hot drink': ('Medicine', 'Red'),
    'hot meat': ('Meat', 'Red'), 'hunter king coin': ('Coin', 'White'), 'huskberry': ('Husk', 'Gray'),
    'immenseness ticket': ('Ticket', 'Blue'), 'immenseness ticket s': ('Ticket', 'Light_Blue'),
    'immunizer': ('Medicine', 'Yellow'), 'iron pickaxe': ('Pickaxe', 'White'),
    'kushala daora ticket': ('Ticket', 'Gray'), 'large barrel': ('Barrel', 'Red'),
    'large beast tear': ('Monster_Part', 'Light_Blue'), 'large toad tear': ('Monster_Part', 'Brown'),
    'large wyvern tear': ('Monster_Part', 'Blue'), 'legend coin g': ('Coin', 'Red'), 'lifecrystals': ('Sac', 'White'),
    'lifepowder': ('Sac', 'White'), 'live bombfish': ('Fish', 'Green'), 'lucky spall': ('Ore', 'Orange'),
    'luminous stomach': ('Sac', 'Orange'), 'map': ('Map', 'White'), 'max potion': ('Sac', 'Yellow'),
    'mega armorskin': ('Medicine', 'Orange'), 'mega bug net': ('Bugnet', 'Yellow'),
    'mega dash juice': ('Medicine', 'Yellow'), 'mega demondrug': ('Medicine', 'Red'),
    'mega fishing fly': ('Bait', 'Pink'), 'mega nutrients': ('Medicine', 'Light_Blue'),
    'mega pickaxe': ('Pickaxe', 'Yellow'), 'mega potion': ('Medicine', 'Green'),
    'meownster ticket': ('Ticket', 'Light_Blue'), 'meteor chunk': ('Ore', 'Blue'), 'might pill': ('Sac', 'Red'),
    'might seed': ('Seed', 'Red'), 'mini whetstone': ('Whetstone', 'Yellow'), 'miracle spall': ('Ore', 'Yellow'),
    'mistentoe': ('Herb', 'Light_Blue'), 'monster guts': ('Monster_Part', 'Yellow'),
    'mosswine jerky': ('Meat', 'Pink'), 'net': ('Spiderweb', 'White'), 'nulberry': ('Seed', 'Blue'),
    'nutrients': ('Medicine', 'Light_Blue'), 'old bug net': ('Bugnet', 'Gray'),
    'old dragon treasure': ('Monster_Part', 'Purple'), 'old pickaxe': ('Pickaxe', 'Gray'),
    'one-shot binder': ('Ammo', 'Orange'), 'paintball': ('Ball', 'Pink'), 'palico ticket': ('Ticket', 'Blue'),
    'paralysis knife': ('Knife', 'Yellow'), 'paw pass ticket': ('Ticket', 'White'),
    'pawprint stamp': ('Ticket', 'White'), 'pinnacle coin': ('Coin', 'Red'),
    'piscine liver': ('Monster_Part', 'Yellow'), 'pitfall trap': ('Trap', 'Green'),
    'poison knife': ('Knife', 'Purple'), 'poison smoke bomb': ('Smoke_Bomb', 'Purple'),
    'poisoned meat': ('Meat', 'Purple'), 'poogie pearl': ('Ball', 'Light_Blue'),
    'pop-eyed goldfish': ('Fish', 'Orange'), 'popo tongue': ('Meat', 'Red'), 'portable spit': ('Spit', 'Orange'),
    'portable steam bomb': ('Smoke_Bomb', 'Yellow'), 'potion': ('Medicine', 'Green'), 'powderstone': ('Ore', 'Red'),
    'powercharm': ('Sac', 'Red'), 'powertalon': ('Claw', 'Red'), 'proto dense marcoal': ('Coin', 'Yellow'),
    'proto wystone: drive': ('Wystone_Drive', 'Purple'), 'psychoserum': ('Medicine', 'Orange'),
    'queen substance': ('Medicine', 'Yellow'), "quester's ticket": ('Ticket', 'Yellow'),
    'rare steak': ('Meat', 'Orange'), 'ration': ('Meat', 'Orange'), 'raw meat': ('Meat', 'Red'),
    'ripened mushroom': ('Mushroom', 'Green'), 'round acorn': ('Seed', 'Green'), 'royal rhino': ('Bug', 'Light_Blue'),
    'secret stash': ('Seed', 'Green'), 'shock trap': ('Trap', 'Purple'), 'silver guild ticket': ('Ticket', 'White'),
    'silverfish': ('Fish', 'White'), 'slagtoth oil': ('Medicine', 'Green'), 'sleep knife': ('Knife', 'Light_Blue'),
    'small barrel': ('Barrel', 'Yellow'), 'smoke bomb': ('Smoke_Bomb', 'White'), 'snake coin g': ('Coin', 'Yellow'),
    'sonic bomb': ('Ball', 'Gray'), 'sootstone ore': ('Ore', 'Gray'), 'spider web': ('Spiderweb', 'White'),
    'stone': ('Ore', 'Gray'), 'sulfur crystal': ('Ore', 'Yellow'), 'sushifish': ('Fish', 'Orange'),
    'sushifish bait': ('Bait', 'Orange'), 'teostra ticket': ('Ticket', 'Red'), 'territorial dung': ('Dung', 'Orange'),
    'territorial dung+': ('Dung', 'Orange'), 'throwing knife': ('Knife', 'White'), 'tinged meat': ('Meat', 'Yellow'),
    'tiny spall': ('Ore', 'White'), 'toad coin': ('Coin', 'Light_Blue'), 'toad coin g': ('Coin', 'Light_Blue'),
    'toad tear': ('Monster_Part', 'Brown'), 'torch': ('Torch', 'Red'), 'tranq bomb': ('Ball', 'Red'),
    'tranq knife': ('Knife', 'Red'), 'tranquilizer': ('Medicine', 'Red'), 'trap tool': ('Box', 'Green'),
    'tropical berry': ('Seed', 'Red'), 'unique mushroom': ('Mushroom', 'White'),
    've bronze ticket': ('Ticket', 'Gray'), 've cosmic ticket': ('Ticket', 'Light_Blue'),
    've deluxe ticket': ('Ticket', 'Green'), 've golden ticket': ('Ticket', 'Yellow'),
    've silver ticket': ('Ticket', 'White'), 've ticket': ('Ticket', 'Green'), 'vermin coin': ('Coin', 'Green'),
    'vermin coin g': ('Coin', 'Green'), 'voucher': ('Ticket', 'White'), 'well-done steak': ('Meat', 'Orange'),
    'whetfish': ('Fish', 'Yellow'), 'whetstone': ('Whetstone', 'Yellow'), 'white liver': ('Meat', 'White'),
    'worm': ('Bait', 'Gray'), 'wystone lv1: brace': ('Wystone_Brace', 'Orange'),
    'wystone lv1: drive': ('Wystone_Drive', 'Purple'), 'wystone lv1: element': ('Wystone_Element', 'Light_Blue'),
    'wystone lv1: force': ('Wystone_Force', 'Red'), 'wystone lv2: brace': ('Wystone_Brace', 'Orange'),
    'wystone lv2: drive': ('Wystone_Drive', 'Purple'), 'wystone lv2: element': ('Wystone_Element', 'Light_Blue'),
    'wystone lv2: force': ('Wystone_Force', 'Red'), 'wyvern egg': ('Egg', 'White'),
    'wyvern tear': ('Monster_Part', 'Blue'),
}


def classify(i, names, byname):
    """Ikon dari wiki (per nama) didahulukan; grup tetap dari klasifikasi dasar."""
    g, cats, col = classify_base(i, names, byname)
    n = (names[i] if i < len(names) else "").lower().strip()
    w = WIKI_ICON.get(n)
    if w and g != "misc":
        return g, [w[0]], w[1]
    return g, cats, col


def resolve(cats, color):
    """Pilih file ikon yang benar-benar ada. '' bila tidak ada -> lencana teks."""
    for c in cats:
        have = AVAIL.get(c)
        if have:
            return "%s|%s" % (c, color if color in have else sorted(have)[0])
    return ""


def build_meta():
    byname = {}
    for i, nm in enumerate(NAMES):
        byname.setdefault(nm.lower().strip(), i)
    ic, gr = [], []
    for i in range(len(NAMES)):
        g, cats, col = classify(i, NAMES, byname)
        if g != "misc" or NAMES[i].lower().strip() not in ("dummy", "invalid message", "(none)", "--------") and not NAMES[i].lower().startswith("dummy"):
            if not resolve(cats, col):
                cats, col = ["Question_Mark"], "Gray"
        ic.append(resolve(cats, col) if cats else "")
        gr.append(g)
    META["ic"], META["gr"] = ic, gr


def load_save(raw, path, fname):
    if len(raw) != SAVE_SIZE:
        raise ValueError("Ukuran file %d, seharusnya %d. Bukan file userX MH4U?" % (len(raw), SAVE_SIZE))
    plain = decrypt_save(raw)
    hdr, _, cs = struct.unpack_from("<HHI", plain, 0)
    if hdr != 16 or cs != (sum(plain[8:]) & M32):
        raise ValueError("Header/checksum tidak cocok, file tidak dikenali.")
    ST.update(raw=raw, plain=plain, path=path, fname=fname)


def build(slots, zenny=None):
    if not isinstance(slots, list) or len(slots) > BOX_SLOTS:
        raise ValueError("Data slot tidak valid.")
    plain = bytearray(ST["plain"])
    if zenny is not None:
        zenny = int(zenny)
        if not 0 <= zenny <= ZENNY_MAX:
            raise ValueError("Zenny harus 0-%d." % ZENNY_MAX)
        struct.pack_into("<I", plain, ZENNY_OFF, zenny)
    for i in range(BOX_SLOTS):
        iid, cnt = slots[i] if i < len(slots) else (0, 0)
        iid, cnt = int(iid), int(cnt)
        if i < len(slots) and not (1 <= iid <= MAX_ID and 1 <= cnt <= 99):
            raise ValueError("Slot %d: ID harus 1-%d, jumlah 1-99." % (i + 1, MAX_ID))
        struct.pack_into("<HH", plain, BOX_OFF + i * 4, iid, cnt)
    out = encrypt_save(plain)
    chk = decrypt_save(out)
    if read_box(chk) != [[int(a), int(b)] for a, b in slots]:
        raise ValueError("Verifikasi gagal, tidak ditulis.")
    if zenny is not None and read_zenny(chk) != zenny:
        raise ValueError("Verifikasi zenny gagal, tidak ditulis.")
    return out


# ------------------------------------------------------------ HTTP ---------
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json", cache="no-store"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "text" in ctype or "json" in ctype else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 5_000_000:
            raise ValueError("Terlalu besar.")
        return self.rfile.read(n)

    def state(self):
        ok = ST["plain"] is not None
        return {"loaded": ok, "fname": ST["fname"], "canSave": bool(ST["path"]),
                "slots": read_box(ST["plain"]) if ok else [], "names": NAMES,
                "zenny": read_zenny(ST["plain"]) if ok else 0, "zmax": ZENNY_MAX,
                "ic": META["ic"], "gr": META["gr"], "groups": GROUPS}

    def do_GET(self):
        u = urlparse(self.path).path
        if u == "/":
            self.send(200, PAGE, "text/html")
        elif u == "/api/state":
            with LOCK:
                self.send(200, self.state())
        elif u.startswith("/icon/"):
            f = u[6:]
            fp = os.path.join(ICON_DIR, f)
            if ICON_RE.match(f) and os.path.isfile(fp):
                with open(fp, "rb") as fh:
                    self.send(200, fh.read(), "image/png", "public, max-age=86400")
            else:
                self.send(404, {"error": "ikon tidak ada"})
        else:
            self.send(404, {"error": "tidak ada"})

    def do_POST(self):
        u = urlparse(self.path)
        try:
            with LOCK:
                if u.path == "/api/load":
                    name = parse_qs(u.query).get("name", ["user1"])[0][:60]
                    load_save(self.body(), None, name)
                    self.send(200, self.state())
                elif u.path == "/api/names":
                    NAMES[:] = parse_names(self.body().decode("utf-8", "replace"))
                    build_meta()
                    self.send(200, {"count": len(NAMES), "names": NAMES, "ic": META["ic"], "gr": META["gr"]})
                elif u.path in ("/api/save", "/api/export"):
                    if ST["plain"] is None:
                        raise ValueError("Belum ada file dimuat.")
                    req = json.loads(self.body())
                    out = build(req["slots"], req.get("zenny"))
                    if u.path == "/api/export":
                        self.send(200, out, "application/octet-stream")
                    else:
                        if not ST["path"]:
                            raise ValueError("File dimuat lewat upload; pakai Unduh.")
                        bak = "%s.bak_%s" % (ST["path"], time.strftime("%Y%m%d_%H%M%S"))
                        open(bak, "wb").write(ST["raw"])
                        open(ST["path"], "wb").write(out)
                        ST["raw"] = out
                        ST["plain"] = decrypt_save(out)
                        self.send(200, {"ok": True, "backup": os.path.basename(bak)})
                else:
                    self.send(404, {"error": "tidak ada"})
        except Exception as e:
            self.send(400, {"error": str(e)})


PAGE = r"""<!doctype html><html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>MH4U Save Editor</title>
<style>
:root{--bg:#14120f;--card:#1e1b17;--cell:#2a2620;--cell2:#37322a;--bd:#3f392f;--tx:#f1ece2;--mu:#a39b8c;--ac:#e0a53a;--acx:#1a1408;--bad:#e0735a;--ok:#6cc47f}
@media(prefers-color-scheme:light){:root{--bg:#f1ece0;--card:#fffdf8;--cell:#e6dec9;--cell2:#d9cfb6;--bd:#cbbfa4;--tx:#2a2620;--mu:#7b7466;--ac:#a5701a;--acx:#fff;--bad:#b4472a;--ok:#2d7a3e}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;font:15px/1.35 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--tx);padding:10px 10px 90px;max-width:780px;margin:auto}
button,input,select{font:inherit;color:inherit}
button{background:var(--ac);color:var(--acx);border:0;border-radius:9px;padding:8px 13px;font-weight:600;cursor:pointer}
button:disabled{opacity:.4}button.g{background:var(--cell);color:var(--tx);border:1px solid var(--bd);font-weight:500}
button.d{background:none;color:var(--bad);border:1px solid var(--bad)}
input,select{background:var(--cell);border:1px solid var(--bd);border-radius:9px;padding:8px 10px;min-width:0}
input[type=file]{width:100%}.mu{color:var(--mu)}small{color:var(--mu)}
.top{display:flex;align-items:center;gap:10px;margin:4px 0 10px}
.logo{width:44px;height:44px;flex:none;display:grid;place-items:center;background:linear-gradient(180deg,var(--cell2),var(--cell));border:1px solid var(--bd);border-radius:11px}
.logo img{width:32px;height:32px}.top h1{font-size:18px;margin:0;line-height:1.1}.top .f{flex:1;min-width:0}
#fn{font-size:12px;color:var(--mu);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:block}
.card{background:var(--card);border:1px solid var(--bd);border-radius:13px;padding:11px;margin:8px 0}
.r{display:flex;gap:6px;align-items:center;margin:6px 0;flex-wrap:wrap}.r>.f{flex:1;min-width:120px}
.tabs{display:flex;gap:6px;margin:6px 0}.tabs button{flex:1;background:var(--cell);color:var(--tx);border:1px solid var(--bd);font-weight:500}
.tabs button.on{background:var(--ac);color:var(--acx);border-color:var(--ac);font-weight:700}
.bar{height:5px;background:var(--cell);border-radius:9px;overflow:hidden;margin:6px 0 2px}.bar i{display:block;height:100%;background:var(--ac);width:0}
.pager{display:flex;gap:6px;align-items:center;margin:8px 0}.pager select{flex:1;text-align:center}.pager button{padding:8px 14px}
/* ---------- grid inventory ---------- */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(56px,1fr));gap:5px}
.cell{position:relative;aspect-ratio:1;display:grid;place-items:center;padding:0;border-radius:9px;
 background:linear-gradient(180deg,var(--cell2),var(--cell));border:1px solid var(--bd);
 box-shadow:inset 0 1px 0 rgba(255,255,255,.07),inset 0 -2px 0 rgba(0,0,0,.18);color:var(--tx);font-weight:400}
button.cell.f:hover,button.cell.t:hover{border-color:var(--ac)}button.cell.f:active{transform:scale(.95)}
.cell.e{background:var(--cell);opacity:.35;box-shadow:none}
.cell.add{border:1px dashed var(--ac);background:none;color:var(--ac);font-size:26px;font-weight:300;box-shadow:none}
.ic{width:48px;height:48px;image-rendering:pixelated;image-rendering:crisp-edges;display:block;pointer-events:none;-webkit-user-drag:none;user-select:none}
.bd{display:grid;place-items:center;border-radius:8px;border:2px solid var(--c);color:var(--c);
 background:color-mix(in srgb,var(--c) 14%,transparent);font:700 11px/1 ui-monospace,monospace;letter-spacing:.5px;pointer-events:none}
.q{position:absolute;right:4px;bottom:2px;font:800 13px/1 system-ui,sans-serif;color:#fff;pointer-events:none;
 text-shadow:-1px -1px 0 #000,1px -1px 0 #000,-1px 1px 0 #000,1px 1px 0 #000,0 0 3px #000}
.cell.t .q{bottom:auto;top:3px}.q.mx{color:#ffd45a}.cell.own .q{color:#bfe9c9}
/* katalog */
.grid.cat{grid-template-columns:repeat(auto-fill,minmax(78px,1fr))}
.cell.t{aspect-ratio:auto;grid-template-rows:auto auto;align-content:start;justify-items:center;gap:3px;padding:7px 3px 5px;min-height:92px}
.cell.t small{font-size:10.5px;line-height:1.15;text-align:center;max-height:2.4em;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;word-break:break-word;color:var(--tx);opacity:.85}
.cell.t.sel{border-color:var(--ac);box-shadow:0 0 0 2px var(--ac),inset 0 1px 0 rgba(255,255,255,.07)}
.cell.t.sel::after{content:"✓";position:absolute;left:4px;top:2px;font-size:13px;font-weight:800;color:var(--ac)}
.chips{display:flex;gap:6px;overflow-x:auto;padding:2px 0 8px;scrollbar-width:thin}
.chips button{flex:none;background:var(--cell);color:var(--tx);border:1px solid var(--bd);font-weight:500;padding:6px 11px;border-radius:99px;font-size:13px}
.chips button.on{background:var(--ac);color:var(--acx);border-color:var(--ac);font-weight:700}
.chips button span{opacity:.6;margin-left:4px;font-size:11px}
/* bar bawah & sheet */
.dock{position:fixed;left:0;right:0;bottom:0;z-index:20;padding:10px calc(10px + env(safe-area-inset-right)) calc(10px + env(safe-area-inset-bottom)) calc(10px + env(safe-area-inset-left));
 background:var(--card);border-top:1px solid var(--bd);box-shadow:0 -6px 20px rgba(0,0,0,.35)}
.dock .in{max-width:780px;margin:auto;display:flex;gap:6px;align-items:center}.dock .in>b{flex:1;min-width:0}
.dock input{width:68px;text-align:center}
.sheet{position:fixed;inset:0;z-index:40;background:rgba(0,0,0,.55);display:flex;align-items:flex-end;justify-content:center}
.sheet[hidden]{display:none}
.sh{width:100%;max-width:440px;background:var(--card);border:1px solid var(--bd);border-radius:18px 18px 0 0;padding:16px 16px calc(16px + env(safe-area-inset-bottom))}
@media(min-width:600px){.sheet{align-items:center}.sh{border-radius:18px}}
.sh .hd{display:flex;gap:14px;align-items:center}.sh .big{width:96px;height:96px;flex:none;display:grid;place-items:center;border-radius:14px;
 background:linear-gradient(180deg,var(--cell2),var(--cell));border:1px solid var(--bd)}.sh .big .ic{width:96px;height:96px}
.sh .big .bd{font-size:22px}.sh h2{margin:0 0 3px;font-size:18px;line-height:1.2}
.step{display:flex;gap:6px;margin:16px 0 10px}.step input{flex:1;text-align:center;font-size:20px;font-weight:700;width:0}
.step button{padding:8px 10px;min-width:44px}
#msg{position:fixed;left:10px;right:10px;top:10px;z-index:60;max-width:760px;margin:auto;padding:10px 12px;border-radius:11px;background:var(--card);border:1px solid var(--bd);box-shadow:0 6px 24px rgba(0,0,0,.4);display:none}
details{margin:8px 0}summary{cursor:pointer;color:var(--mu)}
</style></head><body>
<div id="msg"></div>
<div class="top"><div class="logo"><img src="/icon/MH4U-Box_Icon_Yellow.png" alt="" style="image-rendering:pixelated"></div>
<div class="f"><h1>MH4U Save Editor</h1><span id="fn">belum ada file</span></div>
<button id="sv" hidden>Simpan</button><button id="dl" class="g" hidden>Unduh</button></div>

<div class="card" id="ld"><b>Muat save</b><div class="r"><input type="file" id="fl" class="f"></div>
<small>Pilih file <code>user1</code> (salinan dari folder save Citra / app).</small></div>

<div id="app" hidden>
<div class="card"><div class="r"><b class="f">Zenny</b><input id="zn" type="number" min="0" max="9999999" inputmode="numeric" style="width:150px;text-align:right"><button id="zmx" class="g">Maks</button></div>
<small>Batas game 9.999.999. Nilai lebih besar ditolak.</small></div>
<div class="tabs"><button id="t1" class="on">Isi Box</button><button id="t2">Tambah Item</button></div>

<div id="p1">
 <div class="card">
  <div class="r"><input id="q" class="f" placeholder="Cari nama / ID di box"><button id="srt" class="g" title="Urutkan berdasarkan ID">Urut ID</button><button id="all" class="g">Semua 99</button></div>
  <div class="bar"><i id="bar"></i></div><small id="cnt"></small>
  <div class="pager"><button id="pv" class="g">‹</button><select id="pg"></select><button id="nx" class="g">›</button></div>
  <div id="grid" class="grid"></div>
 </div>
</div>

<div id="p2" hidden>
 <div class="card">
  <div class="r"><input id="cq" class="f" placeholder="Cari nama / ID (mis. potion, bomb, 225)"></div>
  <div class="chips" id="chips"></div>
  <div class="r"><button id="pall" class="g">Pilih semua hasil</button><button id="pclr" class="g">Kosongkan pilihan</button></div>
  <div class="r"><b id="selc2" class="f">0 item dipilih</b><input id="n3" type="number" min="1" max="99" value="99" style="width:70px"><button id="seladd2" disabled>Tambah</button></div>
  <div id="cat" class="grid cat"></div>
  <div class="r"><button id="more" class="g f" hidden>Tampilkan lagi</button></div>
 </div>
 <div class="card"><details><summary>Tambah lewat ID / ganti daftar nama</summary>
  <div class="r"><input id="ids" class="f" placeholder="mis. 8,9,20-30"><input id="n1" type="number" min="1" max="99" value="99" style="width:70px"><button id="ad">Tambah</button></div>
  <small>ID yang sudah ada: jumlahnya diubah. ID 1-1935.</small>
  <div class="r"><input type="file" id="nf" class="f"></div>
  <small>Daftar nama bawaan sudah terpasang; boleh diganti dengan <code>itemlist.txt</code> sendiri. Ikon baru cukup ditaruh di folder <code>icon/</code> lalu restart server.</small>
 </details></div>
</div>
</div>

<div class="dock" id="selbar" hidden><div class="in"><b id="selc"></b><input id="n2" type="number" min="1" max="99" value="99"><button id="seladd">Tambah</button><button id="selx" class="g">Batal</button></div></div>
<div class="dock" id="replbar" hidden><div class="in"><b id="replt"></b><button id="replx" class="g">Batal</button></div></div>

<div class="sheet" id="sheet" hidden><div class="sh">
 <div class="hd"><div class="big" id="si"></div><div class="f" style="min-width:0"><h2 id="sn"></h2><small id="sd"></small></div></div>
 <div class="step"><button class="g" data-d="-10">−10</button><button class="g" data-d="-1">−1</button><input id="sc" type="number" min="1" max="99" inputmode="numeric"><button class="g" data-d="1">+1</button><button class="g" data-d="10">+10</button></div>
 <div class="r"><button id="s99" class="g f">Maks (99)</button><button id="srep" class="g f">Ganti item</button></div>
 <div class="r"><button id="sdel" class="d f">Hapus dari box</button><button id="sok" class="f">Selesai</button></div>
</div></div>

<script>
const $=id=>document.getElementById(id);
let slots=[],names=[],ic=[],gr=[],GL=[],GM={},dirty=false,can=false,fname='user1',zenny=0;const ZMAX=9999999;
let page=0,flt='',cq='',cg='all',clim=120,sel=new Set(),repl=-1,cur=-1;
const PS=100,MAXS=1400,MAXID=1935;
const nm=i=>names[i]||('Item #'+i);
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const gm=id=>GM[gr[id]]||GM.misc||{l:'',s:'?',c:'#999'};
function badge(id,px){const g=gm(id);return '<span class="bd" style="--c:'+g.c+';width:'+px+'px;height:'+px+'px">'+g.s+'</span>'}
function icon(id,px){const k=ic[id];if(!k)return badge(id,Math.round(px*.85));const p=k.split('|');
 return '<img class="ic" draggable="false" width="'+px+'" height="'+px+'" data-id="'+id+'" src="/icon/MH4U-'+p[0]+'_Icon_'+p[1]+'.png" alt="">'}
addEventListener('error',e=>{const t=e.target;if(t&&t.tagName==='IMG'&&t.dataset&&t.dataset.id){const s=document.createElement('span');s.innerHTML=badge(+t.dataset.id,Math.round(t.width*.85));t.replaceWith(s.firstChild)}},true);
function msg(t,bad){const m=$('msg');m.textContent=t;m.style.display='block';m.style.color=bad?'var(--bad)':'var(--ok)';clearTimeout(msg.t);msg.t=setTimeout(()=>m.style.display='none',4200)}
async function api(p,opt){const r=await fetch(p,opt);if(!r.ok){let e='Gagal';try{e=(await r.json()).error}catch(_){}throw new Error(e)}return r}
function setDirty(){dirty=true;$('fn').textContent=fname+' •'}
function setMeta(s){names=s.names||names;ic=s.ic||ic;gr=s.gr||gr;if(s.groups){GM={};s.groups.forEach(g=>GM[g[0]]={l:g[1],s:g[2],c:g[3]});GL=s.groups}}
function apply(s){setMeta(s);if(!s.loaded)return;slots=s.slots;zenny=s.zenny||0;$('zn').value=zenny;can=s.canSave;fname=s.fname||'user1';dirty=false;
 $('ld').hidden=true;$('app').hidden=false;$('dl').hidden=false;render();renderCat()}

/* ---------- tab Isi Box ---------- */
function boxIdx(){const q=flt.trim().toLowerCase();if(!q)return null;const o=[];
 for(let i=0;i<slots.length;i++){const id=slots[i][0];if(nm(id).toLowerCase().includes(q)||String(id)===q)o.push(i)}return o}
function cell(i){
 if(i<slots.length){const s=slots[i],id=s[0],c=s[1];
  return '<button class="cell f" data-i="'+i+'" title="'+esc(nm(id))+' (ID '+id+') ×'+c+'">'+icon(id,48)+'<b class="q'+(c>=99?' mx':'')+'">'+c+'</b></button>'}
 if(i===slots.length)return '<button class="cell add" data-add="1" title="Tambah item">+</button>';
 return '<div class="cell e"></div>'}
function render(){
 $('fn').textContent=fname+(dirty?' •':'');$('sv').hidden=!can;
 $('cnt').textContent=slots.length+' / '+MAXS+' slot terpakai';$('bar').style.width=(slots.length/MAXS*100)+'%';
 const idx=boxIdx(),total=idx?idx.length:MAXS,pages=Math.max(1,Math.ceil(total/PS));if(page>=pages)page=pages-1;if(page<0)page=0;
 let h='';
 if(idx){for(const i of idx.slice(page*PS,page*PS+PS))h+=cell(i);if(!idx.length)h='<p class="mu">Tidak ada item yang cocok.</p>'}
 else for(let i=page*PS;i<Math.min(MAXS,page*PS+PS);i++)h+=cell(i);
 $('grid').innerHTML=h;
 let o='';for(let p=0;p<pages;p++){const a=idx?Math.min(PS,idx.length-p*PS):Math.max(0,Math.min(PS,slots.length-p*PS));
  o+='<option value="'+p+'"'+(p===page?' selected':'')+'>Box '+(p+1)+' / '+pages+'  ·  '+a+' item</option>'}
 $('pg').innerHTML=o;$('pv').disabled=page<=0;$('nx').disabled=page>=pages-1}
function goPage(p){page=p;render();$('grid').scrollIntoView({block:'nearest'})}
$('pv').onclick=()=>goPage(page-1);$('nx').onclick=()=>goPage(page+1);$('pg').onchange=e=>goPage(+e.target.value);
$('q').oninput=e=>{flt=e.target.value;page=0;render()};
$('grid').addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;
 if(b.dataset.add){tab(2);return}if(b.dataset.i!==undefined)openSheet(+b.dataset.i)});
$('all').onclick=()=>{if(confirm('Ubah jumlah semua item di box jadi 99?')){slots.forEach(s=>s[1]=99);setDirty();render()}};
$('srt').onclick=()=>{if(confirm('Urutkan semua item di box berdasarkan ID?')){slots.sort((a,b)=>a[0]-b[0]);setDirty();render()}};

/* ---------- sheet edit ---------- */
function openSheet(i){cur=i;updSheet();$('sheet').hidden=false}
function closeSheet(){$('sheet').hidden=true;cur=-1}
function updSheet(){const s=slots[cur],id=s[0];$('si').innerHTML=icon(id,96);$('sn').textContent=nm(id);
 $('sd').textContent='ID '+id+' · Slot '+(cur+1)+' · '+gm(id).l;$('sc').value=s[1]}
function setCount(v){if(cur<0)return;v=Math.min(99,Math.max(1,parseInt(v)||1));slots[cur][1]=v;$('sc').value=v;setDirty();render()}
$('sheet').addEventListener('click',e=>{if(e.target===$('sheet'))closeSheet()});
document.querySelectorAll('.step [data-d]').forEach(b=>b.onclick=()=>setCount(slots[cur][1]+ +b.dataset.d));
$('sc').onchange=e=>setCount(e.target.value);$('s99').onclick=()=>setCount(99);$('sok').onclick=closeSheet;
$('sdel').onclick=()=>{slots.splice(cur,1);closeSheet();setDirty();render();renderCat()};
$('srep').onclick=()=>{repl=cur;closeSheet();sel.clear();tab(2)};
addEventListener('keydown',e=>{if(e.key==='Escape')closeSheet()});

/* ---------- tab Tambah Item ---------- */
const real=i=>{const n=names[i];return n&&!/^dummy\d*$/i.test(n)&&n!=='Invalid Message'&&n!=='(None)'};
function match(i,withG){if(!real(i))return false;if(withG&&cg!=='all'&&(gr[i]||'misc')!==cg)return false;
 const q=cq.trim().toLowerCase();return !q||names[i].toLowerCase().includes(q)||String(i)===q}
function renderCat(){
 const have=new Map();slots.forEach(s=>have.set(s[0],s[1]));
 const cnt={},L=[];let tot=0;
 for(let i=1;i<=MAXID;i++){if(!match(i,false))continue;tot++;const g=gr[i]||'misc';cnt[g]=(cnt[g]||0)+1;if(cg==='all'||g===cg)L.push(i)}
 let c='<button data-g="all" class="'+(cg==='all'?'on':'')+'">Semua<span>'+tot+'</span></button>';
 GL.forEach(g=>{if(cnt[g[0]])c+='<button data-g="'+g[0]+'" class="'+(cg===g[0]?'on':'')+'">'+esc(g[1])+'<span>'+cnt[g[0]]+'</span></button>'});
 $('chips').innerHTML=c;
 let h='';for(const i of L.slice(0,clim)){const own=have.has(i);
  h+='<button class="cell t'+(sel.has(i)?' sel':'')+(own?' own':'')+'" data-id="'+i+'" title="'+esc(nm(i))+' (ID '+i+')">'+icon(i,48)+
   (own?'<b class="q">'+have.get(i)+'</b>':'')+'<small>'+esc(nm(i))+'</small></button>'}
 $('cat').innerHTML=h||'<p class="mu">Tidak ada item yang cocok.</p>';$('more').hidden=L.length<=clim;
 $('more').textContent='Tampilkan lagi ('+(L.length-clim)+')';renderBars();renderCat.L=L}
function renderBars(){
 $('selbar').hidden=!(sel.size&&repl<0);$('selc').textContent=sel.size+' item dipilih';$('selc2').textContent=sel.size+' item dipilih';$('seladd2').disabled=!(sel.size&&repl<0);
 $('replbar').hidden=repl<0;if(repl>=0&&slots[repl])$('replt').textContent='Ganti slot '+(repl+1)+' ('+nm(slots[repl][0])+') – pilih item baru'}
function add(ids,c){let a=0,u=0,f=0;for(const id of ids){const k=slots.findIndex(x=>x[0]===id);
 if(k>=0){slots[k][1]=c;u++}else if(slots.length<MAXS){slots.push([id,c]);a++}else f++}
 setDirty();render();renderCat();msg('Ditambah '+a+', diubah '+u+(f?', '+f+' dilewati (box penuh)':'')+'. Jangan lupa Simpan/Unduh.')}
$('cq').oninput=e=>{cq=e.target.value;clim=120;renderCat()};
$('chips').addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;cg=b.dataset.g;clim=120;renderCat()});
$('more').onclick=()=>{clim+=240;renderCat()};
$('cat').addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;const id=+b.dataset.id;
 if(repl>=0){const k=slots.findIndex(x=>x[0]===id);if(k>=0&&k!==repl){msg('Item itu sudah ada di slot '+(k+1)+'.',1);return}
  slots[repl][0]=id;const at=repl;repl=-1;setDirty();render();renderCat();tab(1);openSheet(at);return}
 sel.has(id)?sel.delete(id):sel.add(id);b.classList.toggle('sel');renderBars()});
$('pall').onclick=()=>{(renderCat.L||[]).slice(0,500).forEach(i=>sel.add(i));renderCat()};
$('pclr').onclick=()=>{sel.clear();renderCat()};
$('selx').onclick=()=>{sel.clear();renderCat()};$('replx').onclick=()=>{repl=-1;renderBars()};
const cnt1=el=>Math.min(99,Math.max(1,parseInt(el.value)||99));
$('seladd').onclick=()=>{add([...sel],cnt1($('n2')));sel.clear();renderCat()};
$('seladd2').onclick=()=>{if(!sel.size)return;add([...sel],cnt1($('n3')));sel.clear();renderCat()};
function parseIds(t){const o=[];for(const p of t.split(',')){const s=p.trim();if(!s)continue;const m=s.match(/^(\d+)(?:-(\d+))?$/);if(!m)throw new Error('Format ID salah: '+s);
 const a=+m[1],b=m[2]?+m[2]:a;if(a<1||b>MAXID||a>b)throw new Error('ID harus 1-'+MAXID);for(let i=a;i<=b;i++)o.push(i);if(o.length>MAXS)throw new Error('Terlalu banyak')}return o}
$('ad').onclick=()=>{try{add(parseIds($('ids').value),cnt1($('n1')))}catch(x){msg(x.message,1)}};

/* ---------- zenny ---------- */
function setZ(v){v=Math.min(ZMAX,Math.max(0,parseInt(v)||0));zenny=v;$('zn').value=v;setDirty()}
$('zn').onchange=e=>setZ(e.target.value);$('zmx').onclick=()=>setZ(ZMAX);

/* ---------- umum ---------- */
function tab(n){$('p1').hidden=n!==1;$('p2').hidden=n!==2;$('t1').className=n===1?'on':'';$('t2').className=n===2?'on':'';
 if(n===1&&repl>=0){repl=-1}renderBars();scrollTo(0,0)}
$('t1').onclick=()=>tab(1);$('t2').onclick=()=>tab(2);
$('fl').onchange=async e=>{const f=e.target.files[0];if(!f)return;try{const r=await api('/api/load?name='+encodeURIComponent(f.name),{method:'POST',body:await f.arrayBuffer()});apply(await r.json());msg('File dimuat.')}catch(x){msg(x.message,1)}};
$('nf').onchange=async e=>{const f=e.target.files[0];if(!f)return;try{const r=await api('/api/names',{method:'POST',body:await f.text()});setMeta(await r.json());render();renderCat();msg('Nama dimuat: '+names.length)}catch(x){msg(x.message,1)}};
$('sv').onclick=async()=>{try{const r=await api('/api/save',{method:'POST',body:JSON.stringify({slots,zenny})});const j=await r.json();dirty=false;render();msg('Tersimpan. Backup: '+j.backup)}catch(x){msg(x.message,1)}};
$('dl').onclick=async()=>{try{const r=await api('/api/export',{method:'POST',body:JSON.stringify({slots,zenny})});const b=await r.blob();const a=document.createElement('a');a.href=URL.createObjectURL(b);a.download=fname;a.click();dirty=false;render();msg('File diunduh.')}catch(x){msg(x.message,1)}};
window.onbeforeunload=()=>dirty?'x':undefined;
api('/api/state').then(r=>r.json()).then(apply);
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("save", nargs="?")
    ap.add_argument("--names")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--lan", action="store_true")
    a = ap.parse_args()
    NAMES[:] = parse_names(NAMES_DATA)
    nf = a.names or ("itemlist.txt" if os.path.exists("itemlist.txt") else None)
    if nf:
        NAMES[:] = parse_names(open(nf, encoding="utf-8", errors="replace").read())
        print("Nama item dari file:", len(NAMES))
    scan_icons()
    build_meta()
    print("Ikon:", sum(len(v) for v in AVAIL.values()), "file,", len(AVAIL), "kategori (folder: %s)" % ICON_DIR)
    if a.save:
        load_save(open(a.save, "rb").read(), a.save, os.path.basename(a.save))
        print("Save dimuat:", a.save)
    host = "0.0.0.0" if a.lan else "127.0.0.1"
    print("Buka di browser: http://127.0.0.1:%d   (Ctrl+C untuk berhenti)" % a.port)
    ThreadingHTTPServer((host, a.port), H).serve_forever()


if __name__ == "__main__":
    main()
