/* 电池包分层爆炸图(参考 registry 的 exploded-stack 做法,改成暗调金属质感)。
   四层:上壳体 / 电芯模组 / 液冷板 / 下护板。画面只由 t 决定。依赖全局 THREE(r147)。
   Battery3D(canvas, { labels: [..4], labelEls: [..4 div], leaders: svg, duration, accent }) -> { render(t) } */
(function () {
  function rounded(w, d, r) {
    var s = new THREE.Shape(), x = -w / 2, y = -d / 2;
    s.moveTo(x + r, y); s.lineTo(x + w - r, y); s.quadraticCurveTo(x + w, y, x + w, y + r);
    s.lineTo(x + w, y + d - r); s.quadraticCurveTo(x + w, y + d, x + w - r, y + d);
    s.lineTo(x + r, y + d); s.quadraticCurveTo(x, y + d, x, y + d - r);
    s.lineTo(x, y + r); s.quadraticCurveTo(x, y, x + r, y);
    return s;
  }
  function slab(w, d, h, mat) {
    var g = new THREE.ExtrudeGeometry(rounded(w, d, 0.22), { depth: h, bevelEnabled: true, bevelThickness: 0.03, bevelSize: 0.03, bevelSegments: 3, curveSegments: 10 });
    g.rotateX(-Math.PI / 2); // 挤出方向朝上
    return new THREE.Mesh(g, mat);
  }
  function canvasTex(draw) {
    var c = document.createElement("canvas"); c.width = 1024; c.height = 640;
    draw(c.getContext("2d"), c.width, c.height);
    var t = new THREE.CanvasTexture(c); t.encoding = THREE.sRGBEncoding; return t;
  }
  function Battery3D(canvas, opt) {
    var W = 1920, H = 1080, T = THREE;
    var accent = new T.Color(opt.accent || "#ff9a3c");
    var r = new T.WebGLRenderer({ canvas: canvas, antialias: true, alpha: true, preserveDrawingBuffer: true });
    r.setPixelRatio(1); r.setSize(W, H, false); r.outputEncoding = T.sRGBEncoding;
    r.toneMapping = T.ACESFilmicToneMapping; r.toneMappingExposure = 0.85;
    var scene = new T.Scene();
    var cam = new T.PerspectiveCamera(28, W / H, 0.1, 100);

    scene.add(new T.HemisphereLight(0x9fb3c8, 0x1a1410, 0.55));
    var key = new T.DirectionalLight(0xfff1e0, 1.0); key.position.set(-6, 9, 7); scene.add(key);
    var rim = new T.DirectionalLight(accent.getHex(), 2.4); rim.position.set(5, 3, -8); scene.add(rim);
    var under = new T.PointLight(accent.getHex(), 1.6, 18); under.position.set(0, -3.2, 2); scene.add(under);

    var PW = 6.2, PD = 3.8;
    var metalDark = new T.MeshStandardMaterial({ color: 0x1d2024, metalness: 0.8, roughness: 0.42 });
    var metalMid = new T.MeshStandardMaterial({ color: 0x5c636c, metalness: 0.7, roughness: 0.32 });
    // 液冷板:蛇形流道画在顶面
    var coolTex = canvasTex(function (g, w, h) {
      g.fillStyle = "#4a5058"; g.fillRect(0, 0, w, h);
      g.strokeStyle = "#ffb469"; g.lineWidth = 16; g.lineCap = "round"; g.lineJoin = "round";
      g.beginPath(); var y = 90; g.moveTo(80, y);
      for (var k = 0; k < 6; k++) { var x2 = k % 2 ? 80 : w - 80; g.lineTo(x2, y); y += 90; if (k < 5) g.lineTo(x2, y); }
      g.stroke();
    });
    var coolTop = new T.MeshStandardMaterial({ map: coolTex, metalness: 0.55, roughness: 0.35, emissive: accent, emissiveIntensity: 0.0 });
    // 上壳体顶面:细分型线 + 品牌无关的铭牌位
    var coverTex = canvasTex(function (g, w, h) {
      g.fillStyle = "#1f2226"; g.fillRect(0, 0, w, h);
      g.strokeStyle = "rgba(255,255,255,0.10)"; g.lineWidth = 4;
      for (var k = 1; k < 8; k++) { g.beginPath(); g.moveTo(k * w / 8, 40); g.lineTo(k * w / 8, h - 40); g.stroke(); }
      g.strokeStyle = "rgba(255,170,90,0.55)"; g.lineWidth = 6; g.strokeRect(24, 24, w - 48, h - 48);
    });

    var layers = [];
    function layer(mesh) { var g = new T.Group(); g.add(mesh); scene.add(g); layers.push(g); return g; }
    // 0 上壳体
    var cover = slab(PW, PD, 0.16, [new T.MeshStandardMaterial({ map: coverTex, metalness: 0.6, roughness: 0.4 }), metalDark]);
    layer(cover);
    // 1 电芯模组:托盘 + 阵列电芯,电芯顶部一条发光极耳
    var tray = slab(PW, PD, 0.1, metalDark);
    var g1 = layer(tray);
    var COLS = 14, ROWS = 4, cw = (PW - 0.6) / COLS, cd = (PD - 0.5) / ROWS;
    var cellGeo = new T.BoxGeometry(cw * 0.84, 0.42, cd * 0.86);
    var cellMat = new T.MeshStandardMaterial({ color: 0x2a2e34, metalness: 0.6, roughness: 0.4 });
    var tabMat = new T.MeshStandardMaterial({ color: accent, emissive: accent, emissiveIntensity: 0.0, metalness: 0.2, roughness: 0.4 });
    var cells = new T.InstancedMesh(cellGeo, cellMat, COLS * ROWS);
    var tabs = new T.InstancedMesh(new T.BoxGeometry(cw * 0.5, 0.03, cd * 0.12), tabMat, COLS * ROWS);
    var m = new T.Matrix4(), n = 0;
    for (var i = 0; i < COLS; i++) for (var j = 0; j < ROWS; j++) {
      var x = -PW / 2 + 0.3 + cw * (i + 0.5), z = -PD / 2 + 0.25 + cd * (j + 0.5);
      m.makeTranslation(x, 0.13 + 0.21, z); cells.setMatrixAt(n, m);
      m.makeTranslation(x, 0.13 + 0.43, z); tabs.setMatrixAt(n, m); n++;
    }
    g1.add(cells, tabs);
    // 2 液冷板
    layer(slab(PW, PD, 0.08, [coolTop, metalMid]));
    // 3 下护板
    layer(slab(PW + 0.2, PD + 0.2, 0.14, metalDark));

    var D = opt.duration || 5;
    var labels = opt.labelEls || [], leaders = opt.leaders;
    var lines = [], dots = [];
    if (leaders) for (var q = 0; q < 4; q++) {
      var ln = document.createElementNS("http://www.w3.org/2000/svg", "line");
      var dt = document.createElementNS("http://www.w3.org/2000/svg", "circle"); dt.setAttribute("r", "6");
      leaders.appendChild(ln); leaders.appendChild(dt); lines.push(ln); dots.push(dt);
    }
    function cl(x) { return Math.min(1, Math.max(0, x)); }
    function eo(x) { x = cl(x); return 1 - Math.pow(1 - x, 3); }
    function eio(x) { x = cl(x); return x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2; }
    var v = new T.Vector3();
    function screen(p) { v.copy(p).project(cam); return [(v.x * 0.5 + 0.5) * W, (-v.y * 0.5 + 0.5) * H]; }
    var BASE_Y = [0.95, 0.52, 0.16, -0.1]; // 合拢时各层高度
    var GAP = [2.15, 1.25, 0.45, 0];        // 爆开后额外抬升
    var target = new T.Vector3(opt.lookX || 1.6, 0.9, 0);
    function render(t) {
      var p = eio((t - 0.35) / 1.5);
      for (var k = 0; k < 4; k++) {
        var pk = eio((t - 0.35 - (3 - k) * 0.06) / 1.4);
        layers[k].position.y = BASE_Y[k] + GAP[k] * pk;
        layers[k].rotation.y = 0.04 * (3 - k) * pk;
      }
      // 极耳与流道在展开后亮起
      var glow = eo((t - 1.4) / 0.8);
      tabMat.emissiveIntensity = 1.6 * glow; coolTop.emissiveIntensity = 0.15 * glow;
      // 镜头:缓慢环绕 + 推近
      var a = -0.62 + 0.32 * (t / D), dist = (opt.dist || 20.5) - 2.4 * eo(t / D), el = 0.5 - 0.08 * (t / D);
      cam.position.set(target.x + Math.sin(a) * dist * Math.cos(el), target.y + Math.sin(el) * dist, Math.cos(a) * dist * Math.cos(el));
      cam.lookAt(target);
      cam.updateMatrixWorld();
      for (var k2 = 0; k2 < labels.length; k2++) {
        // 标注在展开后依次画出,数字砸入前收起,避免和大数字抢画面
        var shown = eo((t - 1.2 - k2 * 0.16) / 0.4) * (1 - eo((t - (opt.labelsOut || 2.75)) / 0.3));
        var sp = screen(new T.Vector3(PW / 2 + 0.05, layers[k2].position.y + 0.08, PD / 2));
        var ax = sp[0], ay = sp[1], lx = ax + 110 * shown;
        labels[k2].style.opacity = shown.toFixed(3);
        labels[k2].style.transform = "translate(" + Math.round(lx + 22) + "px," + Math.round(ay - 26) + "px)";
        if (lines[k2]) {
          lines[k2].setAttribute("x1", ax.toFixed(1)); lines[k2].setAttribute("y1", ay.toFixed(1));
          lines[k2].setAttribute("x2", lx.toFixed(1)); lines[k2].setAttribute("y2", ay.toFixed(1));
          lines[k2].style.opacity = shown.toFixed(3);
          dots[k2].setAttribute("cx", ax.toFixed(1)); dots[k2].setAttribute("cy", ay.toFixed(1));
          dots[k2].style.opacity = shown.toFixed(3);
        }
      }
      r.render(scene, cam);
    }
    render(0);
    return { render: render };
  }
  window.Battery3D = Battery3D;
})();
