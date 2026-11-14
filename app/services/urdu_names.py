"""Best-effort Urdu rendering of names where no curated Urdu name exists.

Transliteration is inherently ambiguous; a catalogue's explicit Urdu name is
always preferable. Preserve punctuation, numbers and non-Latin text unchanged.

The word list below is tuned for how clients and drivers around Gujrat
(Pakistan) are actually written on bills, so "CH. MIRZA SB" reads
چوہدری مرزا صاحب instead of spelled-out letters:

* honorifics first (Chaudhry/``CH.``, Mirza, Malik, Khan, Raja, ... and
  ``SB`` -- the local short form of صاحب),
* common boys' names with their proper Urdu spellings (Asif → آصف, not آسیف),
* clans/surnames of the Gujrat region (Warraich, Gondal, Tarar, ...),
* villages and towns of Gujrat district and its neighbours (Kharian,
  Lalamusa, Jalalpur Jattan, Sarai Alamgir, Dinga, Kunjah, ...),
* walk-in / ledger vocabulary ("OPEN KHATA", "CASH", ...).

Anything not catalogued still falls through to the letter transliterator,
which stays deliberately conservative.
"""
import re

# Whole words are matched case-insensitively *before* every other rule, so a
# walk-in name typed in capitals ("OPEN", "OPEN KHATA", "CASH") is read as the
# word itself and never spelled out letter-by-letter like an initialism.
_WORDS = {
    # ---- Honorifics / titles ("SB" is the local short form of صاحب) ----
    'ch': 'چوہدری', 'chaudhry': 'چوہدری', 'chaudhary': 'چوہدری',
    'choudhry': 'چوہدری', 'chaudri': 'چوہدری',
    'mirza': 'مرزا', 'malik': 'ملک', 'khan': 'خان', 'raja': 'راجہ',
    'mian': 'میاں', 'rao': 'راؤ', 'sheikh': 'شیخ', 'syed': 'سید',
    'sayed': 'سید', 'haji': 'حاجی', 'nawab': 'نواب', 'sardar': 'سردار',
    'hafiz': 'حافظ', 'sahib': 'صاحب', 'sb': 'صاحب',
    # ---- Common boys' names (proper Urdu spellings) ----
    'adnan': 'عدنان', 'aamir': 'عامر', 'ahmad': 'احمد', 'ahmed': 'احمد',
    'amir': 'عامر', 'ajmal': 'اجمل', 'akhtar': 'اختر', 'akram': 'اخرم',
    'ali': 'علی', 'amjad': 'امجد',
    'anwar': 'انور', 'arif': 'عارف', 'arshad': 'ارشد', 'asghar': 'اصغر',
    'ashraf': 'اشرف', 'asif': 'آصف', 'aslam': 'اسلم', 'atif': 'عاطف',
    'awais': 'اویس', 'azam': 'اعظم', 'azhar': 'اظہر', 'babar': 'بابر',
    'bilal': 'بلال', 'dawood': 'داؤد', 'ehmad': 'احمد', 'ehmed': 'احمد',
    'fahad': 'فہد', 'faiz': 'فیض',
    'faisal': 'فیصل', 'fayyaz': 'فیاض', 'ghulam': 'غلام', 'gulzar': 'گلزار',
    'hamid': 'حامد', 'hamed': 'حامد', 'hameed': 'حمید', 'hamza': 'حمزہ',
    'haaris': 'حارث', 'haris': 'حارث',
    'haroon': 'ہارون', 'hasan': 'حسن', 'hassan': 'حسن', 'husain': 'حسین',
    'hussain': 'حسین', 'ibrahim': 'ابراہیم', 'idrees': 'ادریس',
    'iftikhar': 'افتخار', 'ilyas': 'الیاس', 'imran': 'عمران',
    'iqbal': 'اقبال', 'ismail': 'اسماعیل', 'javed': 'جاوید',
    'junaid': 'جنید', 'kamran': 'کامران', 'kashif': 'کاشف',
    'mahmood': 'محمود', 'majeed': 'مجید', 'masood': 'مسعود',
    'mudassir': 'مدثر', 'mudasir': 'مدثر', 'muhammad': 'محمد',
    'muhammed': 'محمد', 'mohammad': 'محمد', 'mohammed': 'محمد',
    'mohmad': 'محمد', 'mushtaq': 'مشتاق',
    'mustafa': 'مصطفیٰ', 'naeem': 'نعیم', 'naseem': 'نسیم', 'nasir': 'ناصر',
    'naveed': 'نوید', 'nawaz': 'نواز', 'noman': 'نعمان', 'omar': 'عمر',
    'osama': 'اسامہ', 'parvez': 'پرویز', 'pervez': 'پرویز', 'qaiser': 'قیصر',
    'rashid': 'راشد', 'rasool': 'رسول', 'riaz': 'ریاض', 'saad': 'سعد',
    'sabir': 'صابر', 'saeed': 'سعید', 'sajjad': 'سجاد', 'salman': 'سلمان',
    'saqib': 'صاقب', 'shabbir': 'شبیر', 'shafiq': 'شفیق', 'shahid': 'شاہد',
    'shahzad': 'شہزاد', 'sharjeel': 'شرجیل', 'shoaib': 'شعیب',
    'sohail': 'سہیل', 'sulaiman': 'سلیمان', 'suleman': 'سلیمان',
    'tahir': 'طاہر', 'talha': 'طلحہ', 'tanveer': 'تنویر', 'tanvir': 'تنویر',
    'tariq': 'طارق', 'umair': 'عمیر', 'umar': 'عمر', 'usama': 'اسامہ',
    'usman': 'عثمان', 'waqas': 'وقاص', 'waseem': 'وسیم', 'wasim': 'وسیم',
    'yaqoob': 'یعقوب', 'yasin': 'یاسین', 'younis': 'یونس', 'yousaf': 'یوسف',
    'yousuf': 'یوسف', 'yusuf': 'یوسف', 'zafar': 'ظفر', 'zahid': 'زاہد',
    'zeeshan': 'ذیشان',
    # ---- Clans / surnames of the Gujrat region ----
    'awan': 'اعوان', 'bhatti': 'بھٹی', 'butt': 'بٹ', 'chadhar': 'چدھڑ',
    'cheema': 'چیمہ', 'gujjar': 'گجر', 'gujar': 'گجر', 'hanjra': 'ہنجڑہ', 'jatt': 'جٹ',
    'gondal': 'گوندل', 'khokhar': 'کھوکھر', 'rajput': 'راجپوت',
    'sandhu': 'سندھو', 'sial': 'سیال', 'tarar': 'ترار', 'virk': 'ورک',
    'warraich': 'وڑائچ', 'waraich': 'وڑائچ',
    # ---- Villages / towns: Gujrat district and neighbours ----
    'gujrat': 'گجرات', 'jalalpur': 'جلالپور', 'jalal': 'جلال', 'pur': 'پور',
    'jattan': 'جٹاں', 'kharian': 'کھاریاں', 'lalamusa': 'لالہ موسیٰ',
    'lala': 'لالہ', 'musa': 'موسیٰ', 'dinga': 'ڈنگہ', 'kunjah': 'کنجاہ',
    'sarai': 'سرائے', 'alamgir': 'عالم گیر', 'tanda': 'ٹنڈہ', 'kotla': 'کوٹلہ',
    'bhagwal': 'بھگوال', 'kakrali': 'ککڑالی', 'mangowal': 'منگووال',
    'shadiwal': 'شادیوال', 'kalaskey': 'کلسکے', 'kalaske': 'کلسکے',
    'bhalot': 'بھلوٹ', 'langrial': 'لنگریال', 'karnana': 'کرنانہ',
    'dhoria': 'ڈھوریہ', 'sobtian': 'سوہتیاں', 'sohtian': 'سوہتیاں',
    'kot': 'کوٹ', 'pind': 'پنڈ', 'chak': 'چک', 'mohallah': 'محلہ',
    'muhallah': 'محلہ', 'nagar': 'نگر', 'cantt': 'کینٹ',
    # ---- Nearby cities / country ----
    'islam': 'اسلام', 'abad': 'آباد', 'wazir': 'وزیر', 'phalia': 'پھالیہ',
    'jhelum': 'جہلم', 'sialkot': 'سیالکوٹ', 'gujranwala': 'گوجرانوالہ',
    'rawalpindi': 'راولپنڈی', 'lahore': 'لاہور', 'mandi': 'منڈی',
    'bahauddin': 'بہاؤالدین', 'mirpur': 'میرپور', 'bhimber': 'بھمبر',
    'kohat': 'کوہاٹ', 'pakistan': 'پاکستان',
    # ---- Walk-in / ledger vocabulary seen in client and driver names ----
    'open': 'اوپن', 'khata': 'کھاتہ', 'cash': 'کیش', 'paid': 'پیڈ',
    'sale': 'سیل', 'credit': 'کریڈٹ', 'advance': 'ایڈوانس', 'rent': 'کرایہ',
    'return': 'ریٹرن', 'due': 'ڈیو', 'general': 'جنرل', 'customer': 'کسٹمر',
    'walking': 'واکنگ', 'site': 'سائٹ', 'shop': 'شاپ', 'store': 'اسٹور',
    'market': 'مارکیٹ', 'trader': 'ٹریڈر', 'traders': 'ٹریڈرز',
    'company': 'کمپنی', 'bank': 'بینک', 'account': 'اکاؤنٹ',
    'booking': 'بکنگ', 'delivery': 'ڈیلیوری', 'payment': 'پیمنٹ',
    'cement': 'سیمنٹ', 'brick': 'اینٹ', 'bricks': 'اینٹ', 'steel': 'سٹیل',
    'chaugath': 'چو گھاٹ', 'chaughat': 'چو گھاٹ', 'chaugat': 'چو گھاٹ',
    'choghat': 'چو گھاٹ', 'choughat': 'چو گھاٹ',
    'mm': 'ملی میٹر', 'kg': 'کلو گرام', 'kgs': 'کلو گرام',
    'kilogram': 'کلو گرام', 'kilograms': 'کلو گرام',
    'goods': 'گڈز', 'material': 'میٹیریل',
    'materials': 'میٹیریلز', 'building': 'بلڈنگ', 'builders': 'بلڈرز',
    'fazal': 'فضل', 'sobtian': 'صوبتیاں', 'sohtian': 'صوبتیاں',
    'driver': 'ڈریور', 'conductor': 'کنڈکٹر',
    # Short all-caps-friendly words (single vowel), so they keep word reading.
    'work': 'ورک', 'home': 'ہوم', 'food': 'فوڈ', 'room': 'روم', 'good': 'گڈ',
    'sons': 'سنز', 'bros': 'بروز', 'mill': 'مل', 'star': 'اسٹار', 'test': 'ٹیسٹ',
    # Known brands / abbreviations kept from the first catalogue.
    'dg': 'ڈی جی',
}
_LETTERS = {
    'a': 'ا', 'b': 'ب', 'c': 'ک', 'd': 'د', 'e': 'ی', 'f': 'ف',
    'g': 'گ', 'h': 'ہ', 'i': 'ی', 'j': 'ج', 'k': 'ک', 'l': 'ل',
    'm': 'م', 'n': 'ن', 'o': 'و', 'p': 'پ', 'q': 'ق', 'r': 'ر',
    's': 'س', 't': 'ت', 'u': 'و', 'v': 'و', 'w': 'و', 'x': 'کس',
    'y': 'ی', 'z': 'ز',
}
_PAIRS = {'sh': 'ش', 'ch': 'چ', 'kh': 'خ', 'gh': 'غ', 'ph': 'ف',
          'th': 'تھ', 'dh': 'دھ', 'bh': 'بھ', 'aa': 'ا', 'ee': 'ی',
          'oo': 'و', 'ai': 'ی', 'ay': 'ے', 'ou': 'او'}

_VOWELS = set('aeiou')


def _looks_like_initialism(text: str) -> bool:
    """A short ALL-CAPS token that reads as letter names, not as a word.

    True initialisms in this trade (``DG``, ``OPC``, ``GRN``, ``HBL``, ``NBP``)
    carry at most one vowel letter.  Words people simply type in capitals --
    ``OPEN``, ``ALI``, ``ASIF`` -- keep their vowel pattern and fall through to
    the word transliterator (or the dictionary above), so the Urdu bill never
    spells them out as "o p e n".
    """
    if not (text.isupper() and 1 < len(text) <= 4):
        return False
    lowered = text.lower()
    return sum(1 for ch in lowered if ch in _VOWELS) <= 1


def _expand_standalone_muhammad(name: str) -> str:
    """Expand standalone ``M`` / ``M.`` (shorthand for Muhammad) to ``محمد``.

    In Pakistani bills and ledgers a lone ``M`` or ``M.`` in a name (``M Ahmed``,
    ``M. Ahmed``, ``M.Ahmed``, ``Ch. M. Ali``) stands for ``محمد``.  Numeric
    units/grades (``3m``, ``M10``, ``M-10``, ``M.10``) and dotted/slashed
    two-letter abbreviations (``M.S.``, ``A.M.``, ``M/S``) are left untouched.
    """
    def repl(match):
        start = match.start()
        has_dot = match.group(0).endswith('.')
        before = name[:start]
        after_letter = name[start + 1:]

        if before and before[-1].isdigit():
            return match.group(0)
        if re.search(r'\d-$', before):
            return match.group(0)
        if re.search(r'(?<![A-Za-z])[A-Za-z][./]$', before):
            return match.group(0)

        if has_dot:
            if len(after_letter) > 1 and after_letter[1].isdigit():
                return match.group(0)
            if re.match(r'^\.[A-Za-z](?![A-Za-z])', after_letter):
                return match.group(0)
            if re.match(r'^\.[A-Za-z]{2,}', after_letter):
                return 'محمد '
            return 'محمد'

        if after_letter and after_letter[0].isdigit():
            return match.group(0)
        if re.match(r'^-\d', after_letter):
            return match.group(0)
        if re.match(r'^/[A-Za-z](?![A-Za-z])', after_letter):
            return match.group(0)
        return 'محمد'

    return re.sub(r'(?<![A-Za-z])[Mm](?![A-Za-z])\.?', repl, name)


def urdu_name(name, preferred=None):
    """Use a saved spelling first; otherwise transliterate Latin words locally."""
    preferred = str(preferred or '').strip()
    if preferred:
        return preferred
    name = _expand_standalone_muhammad(str(name or '').strip())

    def word(match):
        original = match.group()
        text = original.lower()
        if text in _WORDS:
            mapped = _WORDS[text]
            if text in ('mm', 'kg', 'kgs') and match.start() > 0 and name[match.start() - 1].isdigit():
                return ' ' + mapped
            return mapped
        # Initialisms are read as letter names rather than a single word.
        if _looks_like_initialism(original):
            letters = {'a': 'اے', 'b': 'بی', 'c': 'سی', 'd': 'ڈی', 'e': 'ای',
                       'f': 'ایف', 'g': 'جی', 'h': 'ایچ', 'i': 'آئی', 'j': 'جے',
                       'k': 'کے', 'l': 'ایل', 'm': 'ایم', 'n': 'این', 'o': 'او',
                       'p': 'پی', 'q': 'کیو', 'r': 'آر', 's': 'ایس', 't': 'ٹی',
                       'u': 'یو', 'v': 'وی', 'w': 'ڈبلیو', 'x': 'ایکس',
                       'y': 'وائی', 'z': 'زی'}
            return ' '.join(letters[c] for c in text)
        result = []
        i = 0
        while i < len(text):
            pair = text[i:i + 2]
            if pair in _PAIRS:
                result.append(_PAIRS[pair])
                i += 2
            else:
                # Short internal vowels are typically unwritten in Urdu.
                if text[i] not in 'ae' or i == 0 or i == len(text) - 1:
                    result.append(_LETTERS[text[i]])
                i += 1
        return ''.join(result)

    return re.sub(r'[A-Za-z]+', word, name)
