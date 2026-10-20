/* 3D 展厅画廊:官图做成竖立的展板,围成一圈,镜头缓慢推近并绕圈,地面带倒影。
   依赖全局 THREE(r147)。画面只由 t 决定,可任意顺序 seek。
   用法:var g = Gallery3D(canvas, ["a.jpg", ...], { bg: "#0b0d10", ... }); 每帧 g.render(t)。 */
(function () {
  function Gallery3D(canvas, srcs, opt) {
    opt = opt || {};
    var T = window.THREE, W = 1920, H = 1080;
    var bg = new T.Color(opt.bg || "#0b0d10");
    var renderer = new T.WebGLRenderer({ canvas: canvas, antialias: true, preserveDrawingBuffer: true });
    renderer.setPixelRatio(1);
    renderer.setSize(W, H, false);
    renderer.setClearColor(bg, 1);
    renderer.outputEncoding = T.sRGBEncoding;
    var scene = new T.Scene();
    scene.fog = new T.Fog(bg, opt.fogNear || 9, opt.fogFar || 26);
    var camera = new T.PerspectiveCamera(opt.fov || 34, W / H, 0.1, 80);

    // 解码井:<img> 留在 DOM 里让页面 load 等它们解码;尺寸保持原样,纹理按实际像素上传
    var well = document.createElement("div");
    well.style.cssText = "position:absolute;left:0;top:0;width:1px;height:1px;overflow:hidden;opacity:0;pointer-events:none";
    canvas.parentNode.appendChild(well);

    var N = srcs.length, R = opt.radius || 7.2, PW = opt.panelW || 4.2, PH = PW * 9 / 16;
    var ring = new T.Group();
    scene.add(ring);
    var mats = [];
    srcs.forEach(function (src, i) {
      var img = document.createElement("img");
      img.src = src;
      well.appendChild(img);
      var tex = new T.Texture(img);
      tex.encoding = T.sRGBEncoding;
      tex.minFilter = T.LinearFilter;
      tex.generateMipmaps = false;
      // 4:3 官图裁成 16:9 展板:只取中间一段
      tex.repeat.set(1, 0.75);
      tex.offset.set(0, 0.125);
      var mat = new T.MeshBasicMaterial({ map: tex, fog: true });
      var refl = new T.MeshBasicMaterial({ map: tex, fog: true, transparent: true, opacity: opt.reflect || 0.4, side: T.DoubleSide, depthWrite: false });
      function ready() { if (img.naturalWidth) { tex.needsUpdate = true; } }
      img.addEventListener("load", ready);
      ready();
      var a = (i / N) * Math.PI * 2;
      var g = new T.Group();
      var panel = new T.Mesh(new T.PlaneGeometry(PW, PH), mat);
      panel.position.y = PH / 2 + 0.05;
      // 倒影:同一张图上下翻转贴在地面以下
      var mirror = new T.Mesh(new T.PlaneGeometry(PW, PH), refl);
      mirror.scale.y = -1;
      mirror.position.y = -PH / 2 - 0.05;
      // 细边框,让展板像实物
      var frame = new T.Mesh(new T.PlaneGeometry(PW + 0.06, PH + 0.06),
        new T.MeshBasicMaterial({ color: opt.frame || 0x3a4048, fog: true }));
      frame.position.set(0, panel.position.y, -0.01);
      g.add(frame, panel, mirror);
      g.position.set(Math.sin(a) * R, 0, Math.cos(a) * R);
      g.rotation.y = a; // 面朝外
      ring.add(g);
      mats.push(mat);
    });
    // 地面:压暗倒影的渐隐层
    var floor = new T.Mesh(new T.PlaneGeometry(80, 80),
      new T.MeshBasicMaterial({ color: bg, transparent: true, opacity: 0.3, fog: true }));
    floor.rotation.x = -Math.PI / 2;
    floor.position.y = -0.001;
    scene.add(floor);

    var dur = opt.duration || 5;
    var from = opt.camFrom || { dist: 22, height: 2.6, look: 1.1 };
    var to = opt.camTo || { dist: 15.5, height: 1.6, look: 1.2 };
    var spin = opt.spin == null ? 0.55 : opt.spin; // 整段时长里转过的弧度
    var start = opt.startAngle || 0;
    function ease(x) { x = Math.min(1, Math.max(0, x)); return 1 - Math.pow(1 - x, 3); }
    function render(t) {
      var p = ease(t / dur);
      var dist = from.dist + (to.dist - from.dist) * p;
      var h = from.height + (to.height - from.height) * p;
      var look = from.look + (to.look - from.look) * p;
      ring.rotation.y = start + spin * (t / dur);
      camera.position.set(0, h, dist);
      camera.lookAt(0, look, R);
      renderer.render(scene, camera);
    }
    render(0);
    return { render: render, ring: ring, camera: camera };
  }
  window.Gallery3D = Gallery3D;
})();
