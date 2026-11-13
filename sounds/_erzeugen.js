// Erzeugt Wecktöne als WAV (22,05 kHz, 16 bit, mono). Jeder Ton ist eine Schleife,
// die der Wecker endlos wiederholt (Lautstärke-Anstieg macht das Panel bzw. der Player).
// Aufruf: node _erzeugen.js <zielordner>
const fs = require('fs'), path = require('path');
const SR = 22050, out = process.argv[2] || __dirname;
fs.mkdirSync(out, { recursive: true });

const add = (buf, t0, dur, fn) => {
  const s0 = Math.floor(t0 * SR), n = Math.floor(dur * SR);
  for (let i = 0; i < n && s0 + i < buf.length; i++) buf[s0 + i] += fn(i / SR);
};
// Glocke/Marimba: Teiltöne [Faktor, Amplitude, Abklingen]
function bell(buf, t0, f, dur, amp = .5, partials = [[1, 1, 1], [2, .45, 1.6], [2.76, .22, 2.2], [5.4, .1, 3.5]]) {
  add(buf, t0, dur, t => { const att = Math.min(1, t / .004); let v = 0; for (const [m, a, d] of partials) v += a * Math.exp(-t * d * 4.8 / dur) * Math.sin(2 * Math.PI * f * m * t); return amp * att * v; });
}
const MARIMBA = [[1, 1, 1.2], [3.93, .3, 4], [9.2, .08, 7]];
const HARFE = [[1, 1, .9], [2, .5, 1.4], [3, .25, 2], [4, .12, 2.6]];
// digitaler Piepton (Rechteck, weich gefiltert)
function beep(buf, t0, f, dur, amp = .4) {
  add(buf, t0, dur, t => { const e = Math.min(1, t / .005, (dur - t) / .005); const v = Math.sin(2 * Math.PI * f * t) + Math.sin(6 * Math.PI * f * t) / 3 + Math.sin(10 * Math.PI * f * t) / 5; return amp * e * v * .8; });
}
// Pad (Sonnenaufgang): mehrere leicht verstimmte Sinus, langsamer Ein-/Ausklang
function pad(buf, t0, freqs, dur, amp = .25) {
  add(buf, t0, dur, t => { const e = Math.sin(Math.PI * Math.min(1, t / dur)) ** 1.5; let v = 0; for (const f of freqs) v += Math.sin(2 * Math.PI * f * t) + .5 * Math.sin(2 * Math.PI * f * 1.003 * t) + .25 * Math.sin(4 * Math.PI * f * t); return amp * e * v / freqs.length; });
}
// Vogel: schneller Frequenz-Glissando-Triller
function chirp(buf, t0, f0, f1, dur, amp = .35) {
  let ph = 0;
  add(buf, t0, dur, t => { const k = t / dur, f = f0 + (f1 - f0) * k + 260 * Math.sin(2 * Math.PI * 38 * t); ph += 2 * Math.PI * f / SR; return amp * Math.sin(Math.PI * k) * Math.sin(ph); });
}
// mechanische Weckerglocke: Klöppel ~18x/s zwischen zwei Schalen
function schelle(buf, t0, dur) {
  for (let k = 0; k / 18 < dur; k++) bell(buf, t0 + k / 18, k % 2 ? 1320 : 1180, .3, .2, [[1, 1, 9], [2.32, .6, 12], [3.9, .35, 16]]);
}

function tone(name, len, fn) {
  const buf = new Float32Array(Math.floor(len * SR));
  fn(buf);
  let peak = 0; for (const v of buf) peak = Math.max(peak, Math.abs(v));
  const g = .89 / (peak || 1), fade = Math.floor(.03 * SR);
  const pcm = Buffer.alloc(buf.length * 2);
  buf.forEach((v, i) => { const f = Math.min(1, i / fade, (buf.length - i) / fade); pcm.writeInt16LE(Math.round(v * g * f * 32767), i * 2); });
  const h = Buffer.alloc(44);
  h.write('RIFF', 0); h.writeUInt32LE(36 + pcm.length, 4); h.write('WAVE', 8); h.write('fmt ', 12);
  h.writeUInt32LE(16, 16); h.writeUInt16LE(1, 20); h.writeUInt16LE(1, 22); h.writeUInt32LE(SR, 24); h.writeUInt32LE(SR * 2, 28);
  h.writeUInt16LE(2, 32); h.writeUInt16LE(16, 34); h.write('data', 36); h.writeUInt32LE(pcm.length, 40);
  fs.writeFileSync(path.join(out, name + '.wav'), Buffer.concat([h, pcm]));
  console.log(name, (44 + pcm.length) + ' B');
}
const n = s => 440 * 2 ** ((s - 69) / 12);   // MIDI-Nummer → Hz

tone('Sanfter Morgen', 6.4, b => {          // Marimba-Arpeggio in C-Dur, ruhig
  [72, 76, 79, 84, 79, 76, 74, 77, 81, 86, 81, 77].forEach((m, i) => bell(b, i * .5, n(m), 1.3, .45, MARIMBA));
});
tone('Harfe', 6, b => {                      // aufsteigende Harfen-Glissandi
  [60, 64, 67, 72, 76, 79, 84].forEach((m, i) => bell(b, i * .14, n(m), 2.6, .3, HARFE));
  [62, 65, 69, 74, 77, 81, 86].forEach((m, i) => bell(b, 3 + i * .14, n(m), 2.6, .3, HARFE));
});
tone('Sonnenaufgang', 8, b => {              // warme Flächenklänge, sehr sanft
  pad(b, 0, [n(48), n(55), n(64), n(67)], 4.4); pad(b, 3.6, [n(53), n(57), n(65), n(72)], 4.4);
  [84, 88, 91].forEach((m, i) => bell(b, 1 + i * .6, n(m), 2, .12, MARIMBA));
});
tone('Vogelzwitschern', 6, b => {
  [[0, 2600, 3600, .18], [.25, 3400, 2600, .14], [.45, 2800, 4000, .2], [1.6, 3000, 4200, .12], [1.75, 3000, 4200, .12], [1.9, 3000, 4400, .14],
    [3.1, 2400, 3300, .22], [3.4, 3600, 2800, .16], [4.6, 3200, 4300, .1], [4.72, 3200, 4300, .1], [4.84, 3200, 4500, .1], [4.96, 3300, 4600, .16]]
    .forEach(([t, a, z, d]) => chirp(b, t, a, z, d));
});
tone('Glockenspiel', 5.6, b => {
  [79, 84, 88, 91, 88, 84, 86, 91, 95, 91].forEach((m, i) => bell(b, i * .42, n(m), 1.6, .35));
});
tone('Morgenmelodie', 7.2, b => {           // kleine fröhliche Melodie (eigene Komposition)
  const mel = [[72, .4], [74, .4], [76, .4], [79, .8], [76, .4], [79, .4], [84, 1.2], [81, .4], [79, .4], [76, .4], [74, .8], [72, 1.2]];
  let t = 0; for (const [m, d] of mel) { bell(b, t, n(m), d + .9, .4, MARIMBA); bell(b, t, n(m - 12), d + .6, .15, MARIMBA); t += d; }
});
tone('Digital Piep', 2, b => {               // klassischer Digitalwecker: 4× piep, Pause
  for (let i = 0; i < 4; i++) beep(b, i * .16, 2048, .08);
});
tone('Digital Doppelpiep', 1.6, b => {
  beep(b, 0, 1760, .1); beep(b, .18, 1760, .1); beep(b, .6, 2200, .1); beep(b, .78, 2200, .1);
});
tone('Weckerglocke', 3, b => { schelle(b, 0, 1.8); });   // mechanischer Glockenwecker
tone('Gong', 5, b => { bell(b, 0, 196, 4.8, .6, [[1, 1, .6], [1.48, .5, .9], [2.03, .45, 1.2], [2.71, .3, 1.6], [3.6, .2, 2.2], [.51, .3, .5]]); });
