/* 3D 天球(three.js / WebGL)—— 相机在球心朝外看,即"从地球看天空"。
 *
 * 坐标系约定
 * ----------
 * 世界系 = 赤道系:X → (RA 0h, Dec 0°),Y → (RA 6h, Dec 0°),Z → 北天极。
 *   dir(ra,dec) = (cos dec·cos ra, cos dec·sin ra, sin dec)
 * 相机放在原点,up = +Z(北天极朝上)。从球心朝外看时,赤经增大的方向
 * (东)出现在**画面左侧** —— 与项目其它天球图"北上东左"的仰视约定一致。
 *
 * 底图(survey.jpg = ESO eso0932a)是**银道坐标等距柱状**:银心居中、
 * 银经向左增、银纬向上。把它贴到球的内表面时:
 *   - three 的 SphereGeometry 顶点 uv.y=1 在 +Y 极,贴图上边(b=+90°)在 uv.y=1;
 *   - 反解得局部方向 = (cos b·cos l, sin b, cos b·sin l),
 *     即局部 +X → 银心(l=0,b=0),+Y → 北银极,+Z → (l=90°,b=0)。
 * 于是只要把这三个方向的赤道坐标算出来当基向量,就得到"银道系 → 赤道系"的
 * 旋转矩阵(常数与 astro_smb/astro.py 的 galactic_from_radec 同源)。
 *
 * 性能:几何体一次建好,拖动只改相机;sprite 每帧只改 scale/position。
 */
import * as THREE from './three.module.js';

const D2R = Math.PI / 180, R2D = 180 / Math.PI;
const SKY_R = 500;              // 天球(贴图)半径
const DIM_R = SKY_R * 0.96;     // 地平线以下压暗层
const GRID_R = SKY_R * 0.93;    // 赤道网格
const FOOT_R = SKY_R * 0.91;    // 实际视场足迹(在网格之内、标记之外)
const MARK_R = SKY_R * 0.88;    // 目标标记 / 极点标记

// 银道坐标常数(J2000/IAU 1958),与 astro_smb/astro.py 一致
// publish-scan: ok(银道北极的标准常数,不是观测站坐标)
const NGP_RA = 192.85948, NGP_DEC = 27.12825, LON_NCP = 122.93192;

const MIN_FOV = 12, MAX_FOV = 100, DEF_FOV = 62;

// ---------------------------------------------------------------- 与 Python 通信

const bridge = (window.chrome && window.chrome.webview) ? window.chrome.webview : null;

function send(obj) {
  try { if (bridge) bridge.postMessage(obj); } catch (e) { /* 宿主不在 */ }
}

function fatal(msg) {
  const el = document.getElementById('fatal');
  el.textContent = msg;
  el.hidden = false;
  send({ type: 'error', message: msg });
}

window.addEventListener('error', (e) => {
  send({ type: 'error', message: String((e && e.message) || e) });
});
window.addEventListener('unhandledrejection', (e) => {
  send({ type: 'error', message: String((e && e.reason) || e) });
});

// ---------------------------------------------------------------- 天文小工具

function dir(raDeg, decDeg, r) {
  const cd = Math.cos(decDeg * D2R);
  const v = new THREE.Vector3(cd * Math.cos(raDeg * D2R),
                              cd * Math.sin(raDeg * D2R),
                              Math.sin(decDeg * D2R));
  return (r === undefined) ? v : v.multiplyScalar(r);
}

/** 银道 → 赤道(度)。与 astro.radec_from_galactic 同式。 */
function radecFromGalactic(lDeg, bDeg) {
  const l = lDeg * D2R, b = bDeg * D2R;
  const nd = NGP_DEC * D2R, ln = LON_NCP * D2R;
  const sinDec = Math.sin(b) * Math.sin(nd) +
                 Math.cos(b) * Math.cos(nd) * Math.cos(ln - l);
  const dec = Math.asin(Math.max(-1, Math.min(1, sinDec)));
  const ra = NGP_RA * D2R + Math.atan2(
    Math.cos(b) * Math.sin(ln - l),
    Math.sin(b) * Math.cos(nd) - Math.cos(b) * Math.sin(nd) * Math.cos(ln - l));
  return [((ra * R2D) % 360 + 360) % 360, dec * R2D];
}

function galacticToEquatorial() {
  const gx = radecFromGalactic(0, 0);     // 银心
  const gy = radecFromGalactic(0, 90);    // 北银极
  const gz = radecFromGalactic(90, 0);
  return new THREE.Matrix4().makeBasis(
    dir(gx[0], gx[1]), dir(gy[0], gy[1]), dir(gz[0], gz[1]));
}

function fmtRa(deg) {
  let h = ((deg / 15) % 24 + 24) % 24;
  const hh = Math.floor(h);
  const mm = Math.floor((h - hh) * 60);
  return `${String(hh).padStart(2, '0')}h${String(mm).padStart(2, '0')}m`;
}
function fmtDec(deg) {
  const s = deg < 0 ? '-' : '+';
  const v = Math.abs(deg);
  const d = Math.floor(v);
  const m = Math.round((v - d) * 60);
  return `${s}${String(d).padStart(2, '0')}°${String(m).padStart(2, '0')}'`;
}

// ---------------------------------------------------------------- 渲染器

let renderer, scene, camera, stage;
const scalables = [];           // 需要按屏幕像素恒定尺寸缩放的 sprite
const markerSprites = [];       // 参与拾取的目标标记
let needsRender = true;

const state = {
  viewRa: 0, viewDec: 20, fov: DEF_FOV,
  animRa: null, animDec: null, animFov: null,   // 平滑过渡目标
  lat: null, lst: null, showHorizon: false, showGrid: true, showLabels: true,
  zenith: null, hoverName: null, targets: [],
  // `selName` 为 null = 宿主没发过 `targetSelect`(老 UI 就是这样),
  // 此时所有标记都是普通样式
  selName: null,
  showFoot: false, hoverFoot: null, selFoot: null
};

try {
  stage = document.getElementById('stage');
  renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setClearColor(0x070a11, 1);
  stage.appendChild(renderer.domElement);
  scene = new THREE.Scene();
  camera = new THREE.PerspectiveCamera(DEF_FOV, 1, 0.1, 4000);
  camera.up.set(0, 0, 1);       // 北天极朝上
  camera.position.set(0, 0, 0);
} catch (err) {
  fatal('WebGL 初始化失败:' + err + ' — 显卡驱动或硬件加速可能被禁用。');
}
if (!renderer) {
  // 拿不到 WebGL 上下文时后面的建场景代码全会连锁报错,直接中止模块
  throw new Error('WebGL 不可用,3D 天球未初始化');
}

// ---------------------------------------------------------------- 天球贴图

const skyMat = new THREE.MeshBasicMaterial({
  side: THREE.BackSide, color: 0x11151f, depthWrite: false
});
const skyMesh = new THREE.Mesh(new THREE.SphereGeometry(SKY_R, 96, 48), skyMat);
skyMesh.renderOrder = -10;
const galacticGroup = new THREE.Group();
galacticGroup.matrixAutoUpdate = false;
galacticGroup.matrix.copy(galacticToEquatorial());
galacticGroup.add(skyMesh);
scene.add(galacticGroup);

function loadSurvey(url) {
  if (!url) return;
  new THREE.TextureLoader().load(url, (tex) => {
    tex.colorSpace = THREE.SRGBColorSpace;
    tex.anisotropy = Math.min(8, renderer.capabilities.getMaxAnisotropy());
    skyMat.map = tex;
    skyMat.color.setHex(0xffffff);
    skyMat.needsUpdate = true;
    needsRender = true;
    send({ type: 'survey', ok: true });
  }, undefined, () => {
    send({ type: 'survey', ok: false });
  });
}

// ---------------------------------------------------------------- 赤道网格

function lineMat(color, opacity) {
  return new THREE.LineBasicMaterial({
    color, transparent: true, opacity, depthWrite: false
  });
}

const gridGroup = new THREE.Group();
scene.add(gridGroup);

(function buildGrid() {
  const matMinor = lineMat(0x5f7fa8, 0.30);
  const matEq = lineMat(0x7fc2ff, 0.55);
  // 赤经圈:每 2h(30°)一条半圆
  for (let ra = 0; ra < 360; ra += 30) {
    const pts = [];
    for (let dec = -89; dec <= 89; dec += 2) pts.push(dir(ra, dec, GRID_R));
    gridGroup.add(new THREE.Line(
      new THREE.BufferGeometry().setFromPoints(pts), matMinor));
  }
  // 赤纬圈:每 30°(赤道加亮)
  for (let dec = -60; dec <= 60; dec += 30) {
    const pts = [];
    for (let ra = 0; ra <= 360; ra += 3) pts.push(dir(ra, dec, GRID_R));
    gridGroup.add(new THREE.Line(
      new THREE.BufferGeometry().setFromPoints(pts),
      dec === 0 ? matEq : matMinor));
  }
})();

// ---------------------------------------------------------------- sprite 工具

/** 圆环标记贴图(画一次,所有目标共用不同颜色的材质副本)。 */
function ringTexture() {
  const s = 128;
  const c = document.createElement('canvas');
  c.width = c.height = s;
  const g = c.getContext('2d');
  g.strokeStyle = '#ffffff';
  g.lineWidth = 10;
  g.beginPath(); g.arc(s / 2, s / 2, s / 2 - 16, 0, Math.PI * 2); g.stroke();
  g.fillStyle = '#ffffff';
  g.beginPath(); g.arc(s / 2, s / 2, 9, 0, Math.PI * 2); g.fill();
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  return t;
}
const RING_TEX = ringTexture();

/** 十字标记(天极用)。 */
function crossTexture() {
  const s = 128;
  const c = document.createElement('canvas');
  c.width = c.height = s;
  const g = c.getContext('2d');
  g.strokeStyle = '#ffffff';
  g.lineWidth = 8;
  g.beginPath();
  g.moveTo(s / 2, 14); g.lineTo(s / 2, s - 14);
  g.moveTo(14, s / 2); g.lineTo(s - 14, s / 2);
  g.stroke();
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  return t;
}
const CROSS_TEX = crossTexture();

function iconSprite(tex, colorHex, px) {
  const mat = new THREE.SpriteMaterial({
    map: tex, transparent: true, depthTest: false, depthWrite: false,
    color: colorHex
  });
  const sp = new THREE.Sprite(mat);
  sp.renderOrder = 20;
  sp.userData.px = px;
  sp.userData.aspect = 1;
  scalables.push(sp);
  return sp;
}

const LABEL_W = 190, LABEL_H = 26, LABEL_DPR = 2;

/** 文字标签 sprite:画布尺寸固定(便于原地重绘不重建纹理),文字居中。 */
function labelSprite(px) {
  const c = document.createElement('canvas');
  c.width = LABEL_W * LABEL_DPR;
  c.height = LABEL_H * LABEL_DPR;
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  const mat = new THREE.SpriteMaterial({
    map: tex, transparent: true, depthTest: false, depthWrite: false
  });
  const sp = new THREE.Sprite(mat);
  sp.renderOrder = 21;
  sp.userData.px = px || 15;
  sp.userData.aspect = LABEL_W / LABEL_H;
  sp.userData.canvas = c;
  sp.userData.tex = tex;
  scalables.push(sp);
  return sp;
}

function drawLabel(sp, main, suffix, mainColor, subColor) {
  const c = sp.userData.canvas;
  const g = c.getContext('2d');
  g.clearRect(0, 0, c.width, c.height);
  g.textBaseline = 'middle';
  g.shadowColor = 'rgba(0,0,0,0.95)';
  g.shadowBlur = 5 * LABEL_DPR;
  const fMain = `600 ${13 * LABEL_DPR}px "Microsoft YaHei","Segoe UI",sans-serif`;
  const fSub = `500 ${12 * LABEL_DPR}px "Consolas","Microsoft YaHei",monospace`;
  g.font = fMain;
  const wMain = g.measureText(main).width;
  g.font = fSub;
  const wSub = suffix ? g.measureText('  ' + suffix).width : 0;
  let x = (c.width - (wMain + wSub)) / 2;
  g.font = fMain;
  g.fillStyle = mainColor || '#eef4ff';
  g.fillText(main, x, c.height / 2);
  if (suffix) {
    g.font = fSub;
    g.fillStyle = subColor || '#9fb4cc';
    g.fillText('  ' + suffix, x + wMain, c.height / 2);
  }
  sp.userData.tex.needsUpdate = true;
}

// ---------------------------------------------------------------- 天极标记

(function buildPoles() {
  const specs = [[90, '北天极', 0x7fc2ff],
                 [-90, '南天极', 0x7fc2ff]];
  for (const [dec, name, color] of specs) {
    const p = dir(0, dec, MARK_R);
    const ic = iconSprite(CROSS_TEX, color, 16);
    ic.position.copy(p);
    ic.material.opacity = 0.75;
    scene.add(ic);
    const lb = labelSprite(13);
    lb.position.copy(p);
    lb.userData.anchor = p.clone();
    lb.userData.offsetPx = 15;
    drawLabel(lb, name, '', '#a8c8e8', '#7f97ad');
    lb.material.opacity = 0.8;
    scene.add(lb);
  }
})();

// ---------------------------------------------------------------- 目标标记

const targetGroup = new THREE.Group();
scene.add(targetGroup);

function clearTargets() {
  for (const obj of targetGroup.children.slice()) {
    targetGroup.remove(obj);
    const i = scalables.indexOf(obj);
    if (i >= 0) scalables.splice(i, 1);
    if (obj.material) {
      if (obj.userData.tex) obj.userData.tex.dispose();
      obj.material.dispose();
    }
  }
  markerSprites.length = 0;
}

// 选中态:更大 + 亮环。与降级的 QPainter 正射球同一套视觉语言
// (那边是"半径 6 而不是 4,外加一圈 TEXT 色描边")。
const MARK_PX = 20, MARK_SEL_PX = 32, MARK_SEL_COLOR = 0xffffff;

function setTargets(items) {
  clearTargets();
  state.targets = items || [];
  for (const it of state.targets) {
    const color = new THREE.Color(it.color || '#ffd479').getHex();
    const p = dir(it.ra, it.dec, MARK_R);
    const mk = iconSprite(RING_TEX, color, MARK_PX);
    mk.position.copy(p);
    mk.userData.name = it.name;
    mk.userData.basePx = MARK_PX;
    mk.userData.baseColor = color;      // 取消选中要还原成它自己的颜色
    targetGroup.add(mk);
    markerSprites.push(mk);

    const lb = labelSprite(14);
    lb.position.copy(p);
    lb.userData.anchor = p.clone();
    lb.userData.offsetPx = 18;
    lb.userData.name = it.name;
    lb.userData.color = it.color || '#ffd479';
    targetGroup.add(lb);
    mk.userData.label = lb;
  }
  // 重建标记之后要把选中态**重新贴回去** —— 否则换个时刻/夜次重推 targets,
  // 选中的那个就悄悄变回普通标记了
  applyTargetStyle();
  refreshLabels();
  needsRender = true;
}

// 选中态样式。
//
// **宿主不发 `targetSelect` 时它什么也不做** —— 冻结的老 UI 从来不发这条消息,
// 于是 `state.selName` 恒为 null,所有标记都是原来的样子,老 UI 行为一个像素
// 都不变。这是往共享资产里加东西时唯一安全的加法。
function applyTargetStyle() {
  for (const mk of markerSprites) {
    const sel = state.selName !== null && mk.userData.name === state.selName;
    mk.userData.basePx = sel ? MARK_SEL_PX : MARK_PX;
    // 鼠标正悬在上面时别跟它抢 —— hover 有自己的 1.55 倍
    if (state.hoverName !== mk.userData.name) {
      mk.userData.px = mk.userData.basePx;
    }
    mk.material.color.setHex(sel ? MARK_SEL_COLOR : mk.userData.baseColor);
  }
  needsRender = true;
}

const ALT_GOOD = '#7fd88f', ALT_WARN = '#ffc457', ALT_BAD = '#8c98a8';

function altOf(v) {
  if (!state.zenith) return null;
  return Math.asin(Math.max(-1, Math.min(1, v.clone().normalize().dot(state.zenith)))) * R2D;
}

function refreshLabels() {
  for (const mk of markerSprites) {
    const lb = mk.userData.label;
    if (!lb) continue;
    const alt = altOf(mk.position);
    let suffix = '', sub = ALT_BAD;
    if (alt !== null) {
      suffix = `${alt >= 0 ? '' : '-'}${Math.abs(alt).toFixed(0)}°`;
      sub = alt < 0 ? ALT_BAD : (alt < 30 ? ALT_WARN : ALT_GOOD);
    }
    drawLabel(lb, mk.userData.name, suffix, lb.userData.color, sub);
    const faded = (alt !== null && alt < 0);
    mk.material.opacity = faded ? 0.35 : 1.0;
    lb.material.opacity = faded ? 0.45 : 1.0;
    lb.visible = state.showLabels;
  }
  needsRender = true;
}

// ---------------------------------------------------------------- 实际视场足迹
//
// 每张 sub 一个半透明四边形(实为多边形:每条边已在**像素空间**等分,见下)+ 描边。
//
// 为什么直接用宿主给的环点就不会跨 RA=0 / 近极点跑偏
// ------------------------------------------------------
// 宿主(_sky3d.py 的 _footprint_ring)沿图幅边界在**像素空间**等分取样,再逐点
// 过 TAN 投影反算 (ra, dec)。gnomonic 把大圆映成像平面上的直线,所以像素空间的
// 直边取样点**本来就落在同一条大圆上**。这里只把每个 (ra, dec) 独立变成单位向量,
// **全程不对角度做任何插值** —— 于是 RA=0 环绕(359.9 紧挨着 0.1)、赤纬 ±90 附近
// 都不存在特例。**千万别在这儿"顺手"把 ra 排序或 unwrap**,那正是会炸的写法。
//
// 关于弦 vs 弧(照实说):相机就在球心,从球心看时一条弦和它对应的大圆弧落在
// **同一个过原点的平面**里,投影出来是同一条屏幕直线 —— 所以就当前视角而言,
// 只连 4 个角点也不会跑偏。细分是廉价保险:每段 < 90° 让弦的走向永不含糊,
// 且将来若加"从球外看"的视角,4 角点的平面四边形会明显切进球里。
//
// 填充用**加法混合**:重叠处自然变亮 = "这块天区拍了几张",不需要额外图例。

const FOOT_FILL_OP = 0.085;     // 单张填充不透明度(约 12 张叠满)
const FOOT_LINE_OP = 0.45;
const FOOT_SEL_FILL = 0.28;     // 选中/悬停时的高亮
const FOOT_SEL_LINE = 0.95;

const footGroup = new THREE.Group();
scene.add(footGroup);
const footMeshes = [];          // 参与拾取的填充网格
const footById = new Map();

function clearFootprints() {
  for (const obj of footGroup.children.slice()) {
    footGroup.remove(obj);
    if (obj.geometry) obj.geometry.dispose();
    if (obj.material) obj.material.dispose();
  }
  footMeshes.length = 0;
  footById.clear();
}

/** 扁平 [ra0,dec0,ra1,dec1,...] → 单位球面上的顶点(半径 FOOT_R)。 */
function ringPoints(flat) {
  const pts = [];
  for (let i = 0; i + 1 < flat.length; i += 2) pts.push(dir(flat[i], flat[i + 1], FOOT_R));
  return pts;
}

function buildFootprint(item) {
  const pts = ringPoints(item.ring || []);
  if (pts.length < 3) return;
  const color = new THREE.Color(item.color || '#ffd479').getHex();

  // 扇心取顶点的**归一化平均**(球面重心)。视场是凸的,扇形三角化不会自交;
  // 近极点时重心仍在视场内(算的是三维向量,不是赤经均值 —— 那才会炸)。
  const c = new THREE.Vector3();
  for (const p of pts) c.add(p);
  if (c.lengthSq() < 1e-9) return;
  c.normalize().multiplyScalar(FOOT_R);

  const pos = new Float32Array(pts.length * 9);
  for (let i = 0; i < pts.length; i++) {
    const a = pts[i], b = pts[(i + 1) % pts.length];
    const o = i * 9;
    pos[o] = c.x; pos[o + 1] = c.y; pos[o + 2] = c.z;
    pos[o + 3] = a.x; pos[o + 4] = a.y; pos[o + 5] = a.z;
    pos[o + 6] = b.x; pos[o + 7] = b.y; pos[o + 8] = b.z;
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  const mat = new THREE.MeshBasicMaterial({
    color, transparent: true, opacity: FOOT_FILL_OP, side: THREE.DoubleSide,
    depthTest: false, depthWrite: false, blending: THREE.AdditiveBlending
  });
  const mesh = new THREE.Mesh(geo, mat);
  mesh.renderOrder = 4;
  mesh.userData.id = item.id;
  mesh.userData.label = item.label || '';
  mesh.userData.target = item.target || '';
  footGroup.add(mesh);
  footMeshes.push(mesh);

  const lgeo = new THREE.BufferGeometry().setFromPoints(pts);
  const lmat = new THREE.LineBasicMaterial({
    color, transparent: true, opacity: FOOT_LINE_OP,
    depthTest: false, depthWrite: false
  });
  const loop = new THREE.LineLoop(lgeo, lmat);
  loop.renderOrder = 5;
  footGroup.add(loop);
  mesh.userData.loop = loop;
  footById.set(item.id, mesh);
}

function setFootprints(items) {
  clearFootprints();
  for (const it of (items || [])) buildFootprint(it);
  // 换一批足迹后旧的 id 多半已不存在,别让高亮悬在空处
  if (state.selFoot !== null && !footById.has(state.selFoot)) state.selFoot = null;
  if (state.hoverFoot !== null && !footById.has(state.hoverFoot)) state.hoverFoot = null;
  applyFootStyle();
  footGroup.visible = state.showFoot;
  updateFootLegend();
  needsRender = true;
}

/** 图例:加法混合下"越亮 = 叠得越多"不写出来没人猜得到。
 *  元素由 JS 建(sky3d.html 是别的轨道的文件,不在这里改)。 */
function updateFootLegend() {
  let el = document.getElementById('foot-legend');
  if (!el) {
    el = document.createElement('div');
    el.id = 'foot-legend';
    document.body.appendChild(el);
  }
  const n = footMeshes.length;
  el.hidden = !(state.showFoot && n > 0);
  if (!el.hidden) {
    el.textContent = `实际视场 ${n} 张 · 越亮 = 同一块天区叠得越多 · 点四边形看详情`;
  }
}

function applyFootStyle() {
  for (const mk of footMeshes) {
    const hot = (mk.userData.id === state.selFoot) ||
                (mk.userData.id === state.hoverFoot);
    mk.material.opacity = hot ? FOOT_SEL_FILL : FOOT_FILL_OP;
    if (mk.userData.loop) {
      mk.userData.loop.material.opacity = hot ? FOOT_SEL_LINE : FOOT_LINE_OP;
    }
  }
  needsRender = true;
}

function pickFootprint(e) {
  if (!state.showFoot || !footMeshes.length) return null;
  const r = renderer.domElement.getBoundingClientRect();
  ndc.x = ((e.clientX - r.left) / r.width) * 2 - 1;
  ndc.y = -((e.clientY - r.top) / r.height) * 2 + 1;
  ray.setFromCamera(ndc, camera);
  const hits = ray.intersectObjects(footMeshes, false);
  return hits.length ? hits[0].object : null;
}

// ---------------------------------------------------------------- 地平线

const horizonGroup = new THREE.Group();
scene.add(horizonGroup);
let dimMesh = null;

function clearHorizon() {
  for (const obj of horizonGroup.children.slice()) {
    horizonGroup.remove(obj);
    const i = scalables.indexOf(obj);
    if (i >= 0) scalables.splice(i, 1);
    if (obj.userData.tex) obj.userData.tex.dispose();
    if (obj.material) obj.material.dispose();
    if (obj.geometry) obj.geometry.dispose();
  }
  dimMesh = null;
}

function buildHorizon() {
  clearHorizon();
  if (!state.showHorizon || state.zenith === null) { needsRender = true; return; }
  const z = state.zenith;
  const ncp = new THREE.Vector3(0, 0, 1);
  // 正北 = 北天极在地平面上的投影;正东 = 北 × 天顶(右手系校验见 _sky3d.py 注释)
  let north = ncp.clone().addScaledVector(z, -ncp.dot(z));
  if (north.lengthSq() < 1e-6) north = new THREE.Vector3(1, 0, 0);
  north.normalize();
  const east = north.clone().cross(z).normalize();

  const pts = [];
  for (let a = 0; a <= 360; a += 2) {
    const t = a * D2R;
    pts.push(north.clone().multiplyScalar(Math.cos(t))
      .addScaledVector(east, Math.sin(t)).multiplyScalar(GRID_R * 1.01));
  }
  horizonGroup.add(new THREE.Line(
    new THREE.BufferGeometry().setFromPoints(pts), lineMat(0x63e6b8, 0.85)));

  // 地平线以下压暗:以天底为轴的半球(在贴图之内、网格之外)
  const geo = new THREE.SphereGeometry(DIM_R, 48, 24, 0, Math.PI * 2, 0, Math.PI / 2);
  const mat = new THREE.MeshBasicMaterial({
    color: 0x000000, transparent: true, opacity: 0.62,
    side: THREE.BackSide, depthWrite: false
  });
  dimMesh = new THREE.Mesh(geo, mat);
  dimMesh.quaternion.setFromUnitVectors(
    new THREE.Vector3(0, 1, 0), z.clone().negate());
  dimMesh.renderOrder = -5;
  horizonGroup.add(dimMesh);

  // 四方位标(BMP 汉字,画在 canvas 上,不受 HSTRING 限制)
  const cards = [[north, '北'], [east, '东'],
                 [north.clone().negate(), '南'],
                 [east.clone().negate(), '西']];
  for (const [v, name] of cards) {
    const lb = labelSprite(14);
    const p = v.clone().multiplyScalar(GRID_R * 1.01);
    lb.position.copy(p);
    lb.userData.anchor = p.clone();
    lb.userData.offsetPx = 14;
    drawLabel(lb, name, '', '#63e6b8', '#63e6b8');
    horizonGroup.add(lb);
  }
  // 天顶标记
  const zn = iconSprite(CROSS_TEX, 0x63e6b8, 14);
  zn.position.copy(z.clone().multiplyScalar(MARK_R));
  zn.material.opacity = 0.8;
  horizonGroup.add(zn);
  needsRender = true;
}

function setSite(lat, lst, showHorizon) {
  state.lat = lat;
  state.lst = lst;
  state.showHorizon = !!showHorizon;
  state.zenith = (lat === null || lat === undefined || lst === null || lst === undefined)
    ? null : dir(lst, lat).normalize();
  buildHorizon();
  refreshLabels();
}

// ---------------------------------------------------------------- 相机 / 交互

function applyCamera() {
  camera.fov = state.fov;
  camera.updateProjectionMatrix();
  camera.lookAt(dir(state.viewRa, state.viewDec, 10));
  needsRender = true;
  viewDirty = true;
}

function setView(ra, dec, fov, animate) {
  const d = Math.max(-88, Math.min(88, dec));
  if (animate) {
    // 取最近的等价赤经,避免绕远路
    let target = ra;
    while (target - state.viewRa > 180) target -= 360;
    while (target - state.viewRa < -180) target += 360;
    state.animRa = target;
    state.animDec = d;
    state.animFov = (fov === undefined || fov === null) ? state.fov : fov;
  } else {
    state.viewRa = ra; state.viewDec = d;
    if (fov !== undefined && fov !== null) state.fov = fov;
    state.animRa = state.animDec = state.animFov = null;
    applyCamera();
  }
  needsRender = true;
}

let dragging = false, lastX = 0, lastY = 0, moved = 0;

function onDown(e) {
  dragging = true; moved = 0;
  lastX = e.clientX; lastY = e.clientY;
  state.animRa = state.animDec = state.animFov = null;   // 手动接管
  document.body.classList.add('dragging');
  try { renderer.domElement.setPointerCapture(e.pointerId); } catch (_) {}
}

function onMove(e) {
  if (dragging) {
    const dx = e.clientX - lastX, dy = e.clientY - lastY;
    lastX = e.clientX; lastY = e.clientY;
    moved += Math.abs(dx) + Math.abs(dy);
    // 每像素转过的角度 ≈ 视场角 / 画面高度(缩得越近拖得越慢)
    const k = state.fov / Math.max(1, renderer.domElement.clientHeight);
    // 从球心朝外看:赤经增大在画面左侧 ⇒ 向右拖 = 视场中心赤经增大
    state.viewRa = ((state.viewRa + dx * k) % 360 + 360) % 360;
    state.viewDec = Math.max(-88, Math.min(88, state.viewDec + dy * k));
    applyCamera();
    return;
  }
  hoverAt(e);
}

function onUp(e) {
  if (dragging && moved < 4) pickAt(e);
  dragging = false;
  document.body.classList.remove('dragging');
  try { renderer.domElement.releasePointerCapture(e.pointerId); } catch (_) {}
}

function onWheel(e) {
  e.preventDefault();
  const f = Math.exp((e.deltaY > 0 ? 1 : -1) * 0.12);
  state.fov = Math.max(MIN_FOV, Math.min(MAX_FOV, state.fov * f));
  state.animFov = null;
  applyCamera();
}

const ray = new THREE.Raycaster();
const ndc = new THREE.Vector2();

function pickTarget(e) {
  const r = renderer.domElement.getBoundingClientRect();
  ndc.x = ((e.clientX - r.left) / r.width) * 2 - 1;
  ndc.y = -((e.clientY - r.top) / r.height) * 2 + 1;
  ray.setFromCamera(ndc, camera);
  const hits = ray.intersectObjects(markerSprites, false);
  return hits.length ? hits[0].object : null;
}

function hoverAt(e) {
  const obj = pickTarget(e);
  const name = obj ? obj.userData.name : null;
  // 标记优先于足迹:标记就那么几个像素,被大片足迹抢走悬停会很难点中
  const foot = name ? null : pickFootprint(e);
  const footId = foot ? foot.userData.id : null;
  if (name === state.hoverName && footId === state.hoverFoot) return;
  state.hoverName = name;
  if (footId !== state.hoverFoot) {
    state.hoverFoot = footId;
    applyFootStyle();
  }
  for (const mk of markerSprites) {
    mk.userData.px = (mk.userData.name === name) ? mk.userData.basePx * 1.55
                                                 : mk.userData.basePx;
  }
  const hud = document.getElementById('hud-hover');
  if (name) {
    const it = state.targets.find((t) => t.name === name);
    const alt = obj ? altOf(obj.position) : null;
    hud.textContent = it
      ? `${name}   ${fmtRa(it.ra)} ${fmtDec(it.dec)}` +
        (alt === null ? '' : `   高度 ${alt.toFixed(0)}°`)
      : name;
  } else if (foot) {
    hud.textContent = foot.userData.label || foot.userData.target || '';
  } else {
    hud.textContent = '';
  }
  send({ type: 'hover', name });
  needsRender = true;
}

function pickAt(e) {
  const obj = pickTarget(e);
  if (obj) {
    send({ type: 'pick', name: obj.userData.name });
    const it = state.targets.find((t) => t.name === obj.userData.name);
    if (it) setView(it.ra, it.dec, Math.min(state.fov, 34), true);
    return;
  }
  // 点空白处的足迹:高亮它并让宿主在右栏展开这张 sub 的详情(不飞过去 ——
  // 用户多半正想比对相邻几张的重叠,镜头一动就丢了参照)
  const foot = pickFootprint(e);
  if (foot) {
    state.selFoot = foot.userData.id;
    applyFootStyle();
    send({ type: 'footprint', id: foot.userData.id,
           target: foot.userData.target || '' });
  }
}

renderer.domElement.addEventListener('pointerdown', onDown);
renderer.domElement.addEventListener('pointermove', onMove);
renderer.domElement.addEventListener('pointerup', onUp);
renderer.domElement.addEventListener('pointerleave', () => {
  if (state.hoverFoot !== null) {
    state.hoverFoot = null;
    applyFootStyle();
    document.getElementById('hud-hover').textContent = '';
  }
  if (state.hoverName !== null) {
    state.hoverName = null;
    for (const mk of markerSprites) mk.userData.px = mk.userData.basePx;
    document.getElementById('hud-hover').textContent = '';
    send({ type: 'hover', name: null });
    needsRender = true;
  }
});
renderer.domElement.addEventListener('wheel', onWheel, { passive: false });
renderer.domElement.addEventListener('dblclick', () => resetView());
renderer.domElement.addEventListener('contextmenu', (e) => e.preventDefault());

function resetView() {
  if (state.zenith) {
    // 有站点信息:回到天顶(那才是"抬头看到的"方向)
    const z = state.zenith;
    const dec = Math.asin(Math.max(-1, Math.min(1, z.z))) * R2D;
    const ra = ((Math.atan2(z.y, z.x) * R2D) % 360 + 360) % 360;
    setView(ra, dec, DEF_FOV, true);
  } else if (state.targets.length) {
    setView(state.targets[0].ra, state.targets[0].dec, DEF_FOV, true);
  } else {
    setView(0, 20, DEF_FOV, true);
  }
}

// ---------------------------------------------------------------- 每帧

const camUp = new THREE.Vector3();
let lastSent = 0, viewDirty = true;

function updateSprites() {
  const h = renderer.domElement.clientHeight || 1;
  // 单位距离上 1 像素对应的世界尺寸
  const k = 2 * Math.tan(camera.fov * D2R / 2) / h;
  camera.updateMatrixWorld();     // lookAt 后矩阵尚未刷新, 取 up 前先更新
  camUp.setFromMatrixColumn(camera.matrixWorld, 1).normalize();
  for (const sp of scalables) {
    const anchor = sp.userData.anchor;
    const dist = (anchor || sp.position).length();
    if (anchor) {   // 标签:恒定像素偏移挂在锚点下方
      sp.position.copy(anchor).addScaledVector(
        camUp, -(sp.userData.offsetPx || 0) * k * dist);
    }
    const hh = sp.userData.px * k * dist;
    sp.scale.set(hh * sp.userData.aspect, hh, 1);
  }
}

function tick() {
  requestAnimationFrame(tick);
  if (state.animRa !== null) {      // 平滑过渡(点目标/回正)
    const s = 0.16;
    state.viewRa += (state.animRa - state.viewRa) * s;
    state.viewDec += (state.animDec - state.viewDec) * s;
    state.fov += (state.animFov - state.fov) * s;
    if (Math.abs(state.animRa - state.viewRa) < 0.05 &&
        Math.abs(state.animDec - state.viewDec) < 0.05 &&
        Math.abs(state.animFov - state.fov) < 0.05) {
      state.viewRa = ((state.animRa % 360) + 360) % 360;
      state.viewDec = state.animDec;
      state.fov = state.animFov;
      state.animRa = state.animDec = state.animFov = null;
    }
    applyCamera();
  }
  // 视角上报要放在 needsRender 判断**之前**:动画收敛后最后一帧不再渲染,
  // 否则宿主状态栏会永远停在收敛前的读数(真机截图对不上,已修)
  const now = performance.now();
  if (viewDirty && now - lastSent > 250) {
    viewDirty = false;
    lastSent = now;
    send({ type: 'view', ra: state.viewRa, dec: state.viewDec, fov: state.fov });
  }
  if (!needsRender) return;
  needsRender = false;
  updateSprites();
  gridGroup.visible = state.showGrid;
  renderer.render(scene, camera);

  document.getElementById('hud-view').textContent =
    `视场中心 ${fmtRa(state.viewRa)} ${fmtDec(state.viewDec)}   ` +
    `视场 ${state.fov.toFixed(0)}°`;
}

function resize() {
  const w = stage.clientWidth || 1, h = stage.clientHeight || 1;
  // 每次都重设像素比:WebView2 刚建好时 devicePixelRatio 还是 1,
  // 拿到 DPI 后才变 1.25 —— 只在创建时设一次会让高 DPI 屏一直糊(实测)
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  needsRender = true;
}
new ResizeObserver(resize).observe(stage);
window.addEventListener('resize', resize);

// ---------------------------------------------------------------- 宿主消息

function handle(msg) {
  if (!msg || typeof msg !== 'object') return;
  switch (msg.type) {
    case 'init':
      loadSurvey(msg.survey);
      break;
    case 'targets':
      setTargets(msg.items || []);
      break;
    case 'site':
      setSite(msg.lat, msg.lst, msg.showHorizon);
      break;
    case 'footprints':
      if (msg.show !== undefined) state.showFoot = !!msg.show;
      setFootprints(msg.items || []);
      break;
    case 'footSelect':
      state.selFoot = (msg.id === undefined) ? null : msg.id;
      applyFootStyle();
      break;
    case 'targetSelect':
      state.selName = msg.name || null;
      applyTargetStyle();
      break;
    case 'options':
      if (msg.grid !== undefined) state.showGrid = !!msg.grid;
      if (msg.labels !== undefined) {
        state.showLabels = !!msg.labels;
        refreshLabels();
      }
      if (msg.footprints !== undefined) {
        state.showFoot = !!msg.footprints;
        footGroup.visible = state.showFoot;
        if (!state.showFoot && state.hoverFoot !== null) {
          state.hoverFoot = null;
          applyFootStyle();
        }
        updateFootLegend();
      }
      needsRender = true;
      break;
    case 'view':
      setView(msg.ra, msg.dec, msg.fov, msg.animate !== false);
      break;
    case 'reset':
      resetView();
      break;
    default:
      break;
  }
}

if (bridge) bridge.addEventListener('message', (e) => handle(e.data));

// 调试/自动化钩子:没有宿主时(普通浏览器里打开)也能灌数据验证几何
window.__sky3d = {
  handle, state, camera, scene, renderer, dir, radecFromGalactic,
  footMeshes, footById,
  // 足迹三角数(几何是否真的建出来了),自动化校验用
  footTriangles: () => footMeshes.reduce(
    (n, m) => n + m.geometry.getAttribute('position').count / 3, 0),
  project: (ra, dec) => {
    const v = dir(ra, dec, MARK_R).project(camera);
    const r = renderer.domElement.getBoundingClientRect();
    return { x: (v.x + 1) / 2 * r.width, y: (1 - v.y) / 2 * r.height, z: v.z };
  },
  // NDC → 赤道坐标(度),自动化校验贴图对位用
  unproject: (nx, ny) => {
    const v = new THREE.Vector3(nx, ny, 0.5).unproject(camera).normalize();
    return [((Math.atan2(v.y, v.x) * R2D) % 360 + 360) % 360,
            Math.asin(Math.max(-1, Math.min(1, v.z))) * R2D];
  }
};

resize();
applyCamera();
tick();
send({ type: 'ready' });
