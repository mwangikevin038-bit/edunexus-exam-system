from students.models import Student

# New admission numbers for Grade 8 (from the results list PDF, name-matched)
g8_pdf = """242,Salim Masha
249,Chizi Ndegwa
262,Kassimu Juma
257,Fatuma Kassim
238,Alex Muindi
333,Mwanatumu Hassan
245,Bawewe Salim
323,Betrace Nyamvula
336,Bahati Mohamed
307,Mwaka Ndoro
284,Issaka Raphael
236,Peter Mutie
228,Aisha Hassan
278,Aisha Mohamed
265,Mariam Salim
280,Salama Kassim
286,Mwanatumu Bakari
264,Umi Suleiman
252,Subira Siamini
274,Agnes Kaindi
263,Susan Musau
335,Halima Abdalla
279,Mwanapili Kassim
227,Asha Hassan
231,Amir Gabriel
230,Caro Wanjeri
310,Hamida Hamisi
367,Furaha Mbeyu
232,Yazid Mbwana
312,Riziki Pembe
259,Hellen Philip
269,Mbodze Chaleo
317,Yahya Bakari
237,Samuel Mutuku
304,Fatuma Hamisi
288,Saumu Adam
277,Hamisi Shabani
291,Paul Chikophe
321,Stanley Mandale
267,Dan Tyson Nyaga
234,Saumu Rashid
248,Rutuba Hamisi
281,Mwanaidi Juma
246,Kassim Wiki
306,Hawa Rashid
256,Uba Mohamed
270,Mbeyu Mwanakombo
239,Munyoki Muthami
268,Changa Kamanza
316,Emmanuel Mwanza
324,Beatrice Mutuku
272,Hassan Bakari
283,Joygracious Nyambura
247,Bakari Ali
251,Nyamvula Mdune
240,Raphael Tsimba
258,Salimu Chiti
296,Halima Nyevu
294,Daniel Ngove
229,Mariam Wesa
322,Nzyoka Joel
273,Julius Njihia
290,Fatuma Changoma
309,Tima Kisira
244,Daudi King'ang'i
300,Hamisi Juma
275,Suleiman Bakari
225,Ayub Nyawa
411,Chiroro Ng'ombe
226,Mohamed Hassan
276,Ali Omari
243,Sufyan Mwakuro
440,Adam Chindoro
235,Twalib Donald
253,Zulekha Suleiman
260,Kamari Bandikwa
266,Fatuma Abdalla
271,Ega Ruwa
233,Mdzomba Nyawa
250,Caroline Kaleche
254,Cecilia Chao
261,Janet Umazi
282,Abdallah Kibwana
285,Mwanamisi Mpesa
289,Ruwa Mwambire
293,Juma Omar
295,Fatuma Pocho
297,Mwanavita Salim
298,Mohamed Masudi
301,Kwekwe Ruwa
302,Ibrahim Ali
303,Mwanasha Hamisi
305,Bakari HUssein
311,Mohamed Hassan
313,Fauzia Baya
315,Rama Said
319,Riziki Abdalla
318,Mesafari Salim
320,Mwanasiti Salim
325,Kombo Juma
326,Julo Husna
287,Mwanamisi juma
241,Kombo Arifu
314,Loice Hamadi
292,Kwekwe Ng'ombe
327,Mary Dzame
255,Sokwia Kadenge"""

g8_numbers = []
for line in g8_pdf.strip().split('\n'):
    adm, _ = line.split(',', 1)
    g8_numbers.append(adm)

print(f'Grade 8 PDF has {len(g8_numbers)} students, {len(set(g8_numbers))} unique adm numbers')

# New admission numbers for Grade 9 (from the mark entry sheet PDF)
g9_pdf = """105,Stellah Syombua
106,Faith Nadzua
107,Kalewa Samson
108,Asha Masha
109,Naomy Mbeyu
110,Asiya Mbeyu
111,Lucky Mlongo
112,Mwanahawa Mlongo
113,Aisha Kanga
114,Naila Abdulrahman
115,Nasma Mohammed
116,Samson Magangi
117,Hamadi Hassan
120,Lilian Sidi
122,Tima Hassan
124,Mary Mwaka
125,Musa Nyanje
126,Daniel Mangale
127,Imran Omar
128,Halfan Hussein
129,Rosemary Kanini
130,Malik Omar
131,Mbeyu Nyota
132,Hussein Nyota
133,Mwanaidi Hamadi
134,Biasha Abdallah
135,Bintihamisi Abdalla
136,Mwanapili Kombo
137,Fatuma Ali
138,Ismael Mwambeyu
139,Mariam Nyamvula
140,Fatuma Mwanzio
143,Peris Mohamed
144,Samir Titus
145,Amina Malau
146,Stephen Chuli
147,Mwinyi Mfaki
149,Rashid Juma
150,Simon Nzambii
151,Isaac Kurera
154,Nasra Mwalimu
155,Fatuma Kombo
156,Loyce Gome
158,Salimu Abdallah
159,Juma Bakari
160,Athuman Omari
162,Nassoro Omari
163,Mwanasha Hassan
164,Nassoro Said
165,Muhsan Omar
166,Mwanasiti Mohamed
167,Abdallah Mwishee
168,Suleiman Nassoro
169,Ushanga Mshiiri
170,Zainabu Kassim
171,Mwanapili Kiroto
172,Asha Limba
173,Zaituni Baya
174,Idris Bakari
176,Disii Heri
179,Chizi Mwero
180,Hassan Waziri
181,Solomon Joho
182,Mwanasiti Said
183,Shee Said
184,Fatuma Salim
185,Mbetsa Hamisi
186,Mwanamisi Juma
187,Mishi Abdalla
188,Athmani Shabani
189,Victor Muthuri
190,Ismael Mwalewa
191,Mwanasiti Matano
192,Christine Luvuno
193,Saumu Mjeni
194,Khalfan Hassan
195,Beatrice Mariga
196,Sarah Mlongo
197,Patience Nzalambi
198,Grace Mwaka
199,Mercey Ndindi
200,Hollness Mulewa
203,Marriam Omar
204,Bakari Abasi
205,Ali Ndoro
206,Mlongo Tsuma
207,William John
210,Joseph Mwendwa
211,Aaron Mutua
212,John Donati
215,Shedrack Kimeu
216,Yafeth Hassan
217,Benson Kimeu
218,Fatuma Juma
219,Emmanuel Morris
224,Zaituni Hamisi
308,Rehema Sidi
329,Joyce Mlongo
342,Saumu Chizi
363,Mohamed Kassim
441,Mary Moti
446,Fatuma Mwaka
447,Zainab Umazi"""

g9_numbers = []
for line in g9_pdf.strip().split('\n'):
    adm, _ = line.split(',', 1)
    g9_numbers.append(adm)

print(f'Grade 9 PDF has {len(g9_numbers)} students, {len(set(g9_numbers))} unique adm numbers')

# Current JSS adm numbers
current = set(Student.all_objects.filter(school_section='JSS').values_list('admission_no', flat=True))
current.discard(None)
current.discard('')
print(f'Current JSS adm numbers in DB: {len(current)}')

# Check conflicts within new numbers
g8_set = set(g8_numbers)
g9_set = set(g9_numbers)
cross = g8_set & g9_set
print(f'Cross conflicts (G8 vs G9 PDF): {sorted(cross) if cross else "NONE"}')

# Check conflicts with current JSS
g8_vs_current = g8_set & current
g9_vs_current = g9_set & current
print(f'Grade 8 PDF adm numbers that ALREADY exist in JSS: {sorted(g8_vs_current) if g8_vs_current else "NONE"}')
print(f'Grade 9 PDF adm numbers that ALREADY exist in JSS: {sorted(g9_vs_current) if g9_vs_current else "NONE"}')

# What is the "current" admission number for each Grade 8 PDF target that's blocked?
print()
print('=== Current occupants of Grade 8 target numbers ===')
for adm in sorted(g8_set & current, key=lambda x: int(x)):
    for s in Student.all_objects.filter(admission_no=adm, school_section='JSS'):
        print(f'  adm={adm} currently held by: {s.class_name} {s.stream} - {s.name} (id={s.id})')

print()
print('=== Current occupants of Grade 9 target numbers ===')
for adm in sorted(g9_set & current, key=lambda x: int(x)):
    for s in Student.all_objects.filter(admission_no=adm, school_section='JSS'):
        print(f'  adm={adm} currently held by: {s.class_name} {s.stream} - {s.name} (id={s.id})')
