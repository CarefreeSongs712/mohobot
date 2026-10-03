// 语义空间：把真实嵌入向量用 UMAP 压缩到 2 维画出来，演示冗余淘汰半径 r0 的覆盖范围。
// UMAP 只保留邻近关系、不保留距离尺度，所以 r0 圆是按圆心附近的局部比例换算的近似圆；
// 近似圆与 n 维真实邻居吻合度太低时改画真实邻居的凸包。真实邻居始终由后端按 n 维距离判定。
// 后端退回 PCA 时投影是正交收缩，r0 圆是精确的（同尺度）。

const POINT_RADIUS = 4;
const HOVER_PX = 10;
const PADDING = 28;
const LOCAL_SCALE_SAMPLES = 15;
const CIRCLE_MIN_AGREEMENT = 0.6;
const HULL_PADDING = 12;

const cssVar = (name, fallback) => {
    const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || fallback;
};

const median = (values) => {
    if (!values.length) return 0;
    const sorted = [...values].sort((a, b) => a - b);
    return sorted[Math.floor(sorted.length / 2)];
};

// Andrew 单调链凸包；少于 3 个点时原样返回。
const convexHull = (pts) => {
    if (pts.length < 3) return pts.slice();
    const sorted = pts.slice().sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    const cross = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
    const build = (list) => {
        const out = [];
        for (const p of list) {
            while (out.length >= 2 && cross(out[out.length - 2], out[out.length - 1], p) <= 0) out.pop();
            out.push(p);
        }
        out.pop();
        return out;
    };
    return build(sorted).concat(build(sorted.slice().reverse()));
};

export function createSemanticMap({ ref, reactive, computed, nextTick, apiFetch, t, showAlert, imagePreviewClient }) {
    const semanticMapOpen = ref(false);
    const semanticLoading = ref(false);
    const semanticError = ref('');
    const semanticData = ref(null);
    const semanticRadius = ref(0);
    const semanticMinNeighbors = ref(3);
    const semanticSelected = ref(-1);
    const semanticDistances = ref(null);
    const semanticDensity = ref([]);
    const semanticPreview = ref(new Set());
    const semanticPreviewInfo = ref(null);
    const semanticSaving = ref(false);
    const semanticCanvas = ref(null);
    const semanticTooltip = reactive({ visible: false, x: 0, y: 0, point: null, thumb: '', density: 0, distance: null });

    let view = null; // { scale, offsetX, offsetY, width, height }
    // 用户缩放 / 平移：在适配视图的屏幕坐标上再做一次缩放和平移。
    let zoom = 1;
    let panX = 0;
    let panY = 0;
    let drag = null;
    let suppressClick = false;
    let densityTimer = null;
    let resizeObserver = null;

    const points = computed(() => semanticData.value?.points || []);

    // 选中圆心后 r0 的覆盖范围：真实邻居（n 维）+ 图上的近似圆或凸包。
    const coverage = computed(() => {
        const dist = semanticDistances.value;
        const idx = semanticSelected.value;
        const pts = points.value;
        if (!dist || idx < 0 || !pts[idx]) return null;
        const centre = pts[idx];
        const r = semanticRadius.value;
        const exact = semanticData.value?.projection === 'pca';
        const flat = (i) => Math.hypot(pts[i].x - centre.x, pts[i].y - centre.y);

        // 局部比例：圆心在 n 维中最近的若干点，其 2 维距离 / n 维距离的中位数。
        let scale = 1;
        if (!exact) {
            const nearest = dist
                .map((d, i) => [d, i])
                .filter(([d, i]) => i !== idx && d > 0)
                .sort((a, b) => a[0] - b[0])
                .slice(0, LOCAL_SCALE_SAMPLES);
            scale = median(nearest.map(([d, i]) => flat(i) / d));
        }
        const radius2d = r * scale;

        const neighbors = new Set();
        const inCircle = new Set();
        for (let i = 0; i < pts.length; i += 1) {
            if (i === idx) continue;
            if (dist[i] <= r) neighbors.add(i);
            if (flat(i) <= radius2d) inCircle.add(i);
        }
        let both = 0;
        inCircle.forEach((i) => { if (neighbors.has(i)) both += 1; });
        const union = neighbors.size + inCircle.size - both;
        const agreement = union ? both / union : 1;
        const mode = exact || neighbors.size === 0 || agreement >= CIRCLE_MIN_AGREEMENT ? 'circle' : 'hull';
        return {
            mode,
            exact,
            radius2d,
            agreement,
            neighbors,
            inCircle,
            inside: neighbors.size,
            overlap: inCircle.size - both,
        };
    });

    const radiusMax = computed(() => {
        const data = semanticData.value;
        if (!data) return 1;
        return Math.max(Number(data.radius_max) || 0, Number(data.auto_radius) * 2 || 0, semanticRadius.value, 0.01);
    });

    const fitView = () => {
        const canvas = semanticCanvas.value;
        if (!canvas || !points.value.length) return;
        // clientWidth/Height 是布局尺寸，不受弹窗打开动画的 transform 缩放影响。
        const rect = { width: canvas.clientWidth, height: canvas.clientHeight };
        if (!rect.width || !rect.height) return;
        const dpr = window.devicePixelRatio || 1;
        canvas.width = Math.max(1, Math.round(rect.width * dpr));
        canvas.height = Math.max(1, Math.round(rect.height * dpr));
        let minX = Infinity; let maxX = -Infinity; let minY = Infinity; let maxY = -Infinity;
        for (const p of points.value) {
            minX = Math.min(minX, p.x); maxX = Math.max(maxX, p.x);
            minY = Math.min(minY, p.y); maxY = Math.max(maxY, p.y);
        }
        const spanX = Math.max(maxX - minX, 1e-6);
        const spanY = Math.max(maxY - minY, 1e-6);
        // 两轴同一比例尺，圆不会被拉成椭圆。
        const scale = Math.min((rect.width - PADDING * 2) / spanX, (rect.height - PADDING * 2) / spanY);
        view = {
            scale,
            width: rect.width,
            height: rect.height,
            offsetX: rect.width / 2 - ((minX + maxX) / 2) * scale,
            offsetY: rect.height / 2 + ((minY + maxY) / 2) * scale,
            dpr,
        };
    };

    const toScreen = (p) => [
        (view.offsetX + p.x * view.scale) * zoom + panX,
        (view.offsetY - p.y * view.scale) * zoom + panY,
    ];

    const drawCoverage = (ctx, centre, cov, color) => {
        ctx.save();
        ctx.fillStyle = 'rgba(128,128,128,0.10)';
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.5;
        ctx.setLineDash([6, 4]);
        if (cov.mode === 'circle') {
            const [cx, cy] = toScreen(centre);
            ctx.beginPath();
            ctx.arc(cx, cy, cov.radius2d * view.scale * zoom, 0, Math.PI * 2);
            ctx.fill();
            ctx.stroke();
        } else {
            const hull = convexHull(
                [centre, ...[...cov.neighbors].map((i) => points.value[i])].map(toScreen)
            );
            ctx.beginPath();
            hull.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
            ctx.closePath();
            // 粗描边 + 圆角拼接，得到带外边距的凸包区域。
            ctx.setLineDash([]);
            ctx.lineJoin = 'round';
            ctx.lineCap = 'round';
            ctx.lineWidth = HULL_PADDING * 2;
            ctx.strokeStyle = 'rgba(128,128,128,0.10)';
            ctx.stroke();
            ctx.fill();
            ctx.lineWidth = 1.5;
            ctx.strokeStyle = color;
            ctx.setLineDash([6, 4]);
            ctx.stroke();
        }
        ctx.restore();
    };

    const draw = () => {
        const canvas = semanticCanvas.value;
        if (!canvas || !view) return;
        const ctx = canvas.getContext('2d');
        ctx.setTransform(view.dpr, 0, 0, view.dpr, 0, 0);
        ctx.clearRect(0, 0, view.width, view.height);

        const normal = cssVar('--text-secondary', '#a1a7b6');
        const dense = cssVar('--gold-primary', '#d4a853');
        const muted = cssVar('--text-muted', '#8a8a8a');
        const danger = cssVar('--danger', '#d9534f');
        const success = cssVar('--success', '#4caf50');
        const text = cssVar('--text-primary', '#eee');

        const k = Number(semanticMinNeighbors.value) || 1;
        const density = semanticDensity.value;
        const preview = semanticPreview.value;
        const selected = semanticSelected.value;
        const centre = selected >= 0 ? points.value[selected] : null;
        const cov = coverage.value;

        if (centre && cov) drawCoverage(ctx, centre, cov, dense);

        points.value.forEach((p, i) => {
            const [x, y] = toScreen(p);
            const isDense = (density[i] ?? p.density) >= k;
            let fill = isDense ? dense : normal;
            let alpha = 0.9;
            // 受保护的表情画成空心圈：它们占据语义空间、计入邻居数，但不会被淘汰。
            let hollow = Boolean(p.protected);
            if (cov && i !== selected) {
                if (cov.neighbors.has(i)) {
                    fill = success;
                    alpha = 1;
                } else {
                    alpha = 0.25;
                    // 落在近似圆里但 n 维上并不在 r0 内的点：画成空心，表示只是投影重叠。
                    if (cov.mode === 'circle' && cov.inCircle.has(i)) hollow = true;
                }
            }
            ctx.globalAlpha = alpha;
            ctx.beginPath();
            ctx.arc(x, y, i === selected ? POINT_RADIUS + 2 : POINT_RADIUS, 0, Math.PI * 2);
            if (hollow && i !== selected) {
                ctx.lineWidth = 1.5;
                ctx.strokeStyle = p.protected ? muted : fill;
                ctx.globalAlpha = Math.max(alpha, 0.6);
                ctx.stroke();
            } else {
                ctx.fillStyle = i === selected ? text : fill;
                ctx.fill();
            }
            if (preview.has(p.hash)) {
                ctx.globalAlpha = 1;
                ctx.lineWidth = 2;
                ctx.strokeStyle = danger;
                ctx.beginPath();
                ctx.arc(x, y, POINT_RADIUS + 3, 0, Math.PI * 2);
                ctx.stroke();
            }
        });
        ctx.globalAlpha = 1;
    };

    const redraw = () => nextTick(() => { fitView(); draw(); });

    const fetchDensity = async () => {
        if (!semanticData.value) return;
        try {
            const res = await apiFetch(`api/semantic-map/density?radius=${encodeURIComponent(semanticRadius.value)}`);
            const data = await res.json();
            if (data?.success) {
                semanticDensity.value = data.density || [];
                draw();
            }
        } catch (e) {
            console.warn('[SemanticMap] density failed', e);
        }
    };

    const scheduleDensity = () => {
        clearTimeout(densityTimer);
        densityTimer = setTimeout(fetchDensity, 250);
    };

    const loadSemanticMap = async () => {
        semanticLoading.value = true;
        semanticError.value = '';
        semanticSelected.value = -1;
        semanticDistances.value = null;
        semanticPreview.value = new Set();
        semanticPreviewInfo.value = null;
        try {
            const res = await apiFetch('api/semantic-map');
            const data = await res.json();
            if (!data?.success) throw new Error(data?.error || 'load failed');
            semanticData.value = data;
            zoom = 1;
            panX = 0;
            panY = 0;
            semanticRadius.value = Number(data.effective_radius || data.radius || 0);
            semanticMinNeighbors.value = Number(data.min_neighbors || 3);
            semanticDensity.value = (data.points || []).map((p) => p.density);
            redraw();
        } catch (e) {
            semanticError.value = e.message || String(e);
        } finally {
            semanticLoading.value = false;
        }
    };

    const openSemanticMap = async () => {
        semanticMapOpen.value = true;
        await nextTick();
        if (!resizeObserver && window.ResizeObserver && semanticCanvas.value) {
            resizeObserver = new ResizeObserver(() => { fitView(); draw(); });
            resizeObserver.observe(semanticCanvas.value);
        }
        await loadSemanticMap();
        setTimeout(() => { fitView(); draw(); }, 450);
    };

    const closeSemanticMap = () => {
        semanticMapOpen.value = false;
        semanticTooltip.visible = false;
        clearTimeout(densityTimer);
        if (resizeObserver) {
            resizeObserver.disconnect();
            resizeObserver = null;
        }
    };

    // 鼠标位置换算到画布的布局坐标（抵消 CSS transform 带来的缩放）。
    const localPoint = (event) => {
        const canvas = semanticCanvas.value;
        const rect = canvas.getBoundingClientRect();
        const sx = canvas.clientWidth / (rect.width || 1);
        const sy = canvas.clientHeight / (rect.height || 1);
        return [(event.clientX - rect.left) * sx, (event.clientY - rect.top) * sy];
    };

    const pointAt = (event) => {
        const canvas = semanticCanvas.value;
        if (!canvas || !view) return -1;
        const [mx, my] = localPoint(event);
        let best = -1;
        let bestDist = HOVER_PX;
        points.value.forEach((p, i) => {
            const [x, y] = toScreen(p);
            const d = Math.hypot(x - mx, y - my);
            if (d < bestDist) { bestDist = d; best = i; }
        });
        return best;
    };

    const onSemanticMouseDown = (event) => {
        if (event.button !== 0) return;
        const [x, y] = localPoint(event);
        drag = { x, y, panX, panY, moved: false };
    };

    const onSemanticMouseUp = () => {
        if (drag?.moved) suppressClick = true;
        drag = null;
    };

    const onSemanticWheel = (event) => {
        if (!view) return;
        const [mx, my] = localPoint(event);
        const next = Math.min(40, Math.max(1, zoom * Math.exp(-event.deltaY * 0.0015)));
        // 以鼠标所在位置为中心缩放
        const bx = (mx - panX) / zoom;
        const by = (my - panY) / zoom;
        zoom = next;
        panX = zoom === 1 ? 0 : mx - bx * zoom;
        panY = zoom === 1 ? 0 : my - by * zoom;
        semanticTooltip.visible = false;
        draw();
    };

    const resetSemanticZoom = () => {
        zoom = 1;
        panX = 0;
        panY = 0;
        draw();
    };

    const onSemanticHover = async (event) => {
        if (drag) {
            const [x, y] = localPoint(event);
            const dx = x - drag.x;
            const dy = y - drag.y;
            if (drag.moved || Math.hypot(dx, dy) > 4) {
                drag.moved = true;
                panX = drag.panX + dx;
                panY = drag.panY + dy;
                semanticTooltip.visible = false;
                draw();
                return;
            }
        }
        const idx = pointAt(event);
        if (idx < 0) {
            semanticTooltip.visible = false;
            return;
        }
        const p = points.value[idx];
        const canvas = semanticCanvas.value;
        const [x, y] = localPoint(event);
        semanticTooltip.x = Math.max(4, Math.min(x + 14, canvas.clientWidth - 190));
        semanticTooltip.y = Math.max(4, Math.min(y + 14, canvas.clientHeight - 230));
        semanticTooltip.density = semanticDensity.value[idx] ?? p.density;
        semanticTooltip.distance = semanticDistances.value && semanticSelected.value >= 0
            ? semanticDistances.value[idx] : null;
        if (semanticTooltip.point?.hash !== p.hash) {
            semanticTooltip.point = p;
            semanticTooltip.thumb = '';
            semanticTooltip.visible = true;
            const data = await imagePreviewClient.loadThumbnail(p.hash);
            if (semanticTooltip.point?.hash === p.hash && data?.url) semanticTooltip.thumb = data.url;
        } else {
            semanticTooltip.visible = true;
        }
    };

    const onSemanticLeave = () => {
        semanticTooltip.visible = false;
        drag = null;
    };

    const onSemanticClick = async (event) => {
        if (suppressClick) {
            suppressClick = false;
            return;
        }
        const idx = pointAt(event);
        if (idx < 0) {
            semanticSelected.value = -1;
            semanticDistances.value = null;
            draw();
            return;
        }
        semanticSelected.value = idx;
        semanticDistances.value = null;
        draw();
        try {
            const res = await apiFetch(`api/semantic-map/distances?hash=${encodeURIComponent(points.value[idx].hash)}`);
            const data = await res.json();
            if (data?.success && semanticSelected.value === idx) {
                semanticDistances.value = data.distances;
                draw();
            }
        } catch (e) {
            console.warn('[SemanticMap] distances failed', e);
        }
    };

    const onSemanticRadiusInput = () => {
        semanticRadius.value = Number(semanticRadius.value) || 0;
        draw();
        scheduleDensity();
    };

    const useAutoRadius = () => {
        semanticRadius.value = Number(semanticData.value?.auto_radius || 0);
        onSemanticRadiusInput();
    };

    const previewSemanticEviction = async () => {
        try {
            const query = `radius=${encodeURIComponent(semanticRadius.value)}&min_neighbors=${encodeURIComponent(semanticMinNeighbors.value)}`;
            const res = await apiFetch(`api/semantic-map/eviction-preview?${query}`);
            const data = await res.json();
            if (!data?.success) throw new Error(data?.error || 'preview failed');
            semanticPreview.value = new Set(data.hashes || []);
            semanticPreviewInfo.value = { count: (data.hashes || []).length, overCap: !!data.over_cap };
            draw();
        } catch (e) {
            showAlert(e.message || String(e), 'error');
        }
    };

    const saveSemanticSettings = async () => {
        semanticSaving.value = true;
        try {
            const res = await apiFetch('api/semantic-map/settings', {
                method: 'POST',
                body: JSON.stringify({ radius: semanticRadius.value, min_neighbors: semanticMinNeighbors.value }),
            });
            const data = await res.json();
            if (!data?.success) throw new Error(data?.error || 'save failed');
            if (semanticData.value) {
                semanticData.value.radius = data.radius;
                semanticData.value.min_neighbors = data.min_neighbors;
            }
            showAlert(t('pages.dashboard.semantic.saved', '已保存为冗余淘汰参数'), 'success');
        } catch (e) {
            showAlert(e.message || String(e), 'error');
        } finally {
            semanticSaving.value = false;
        }
    };

    const formatNumber = (value, digits = 3) => (Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : '-');

    return {
        semanticMapOpen,
        semanticLoading,
        semanticError,
        semanticData,
        semanticRadius,
        semanticMinNeighbors,
        semanticSelected,
        semanticTooltip,
        semanticCanvas,
        semanticPreviewInfo,
        semanticSaving,
        semanticCoverage: coverage,
        semanticRadiusMax: radiusMax,
        openSemanticMap,
        closeSemanticMap,
        loadSemanticMap,
        onSemanticHover,
        onSemanticLeave,
        onSemanticClick,
        onSemanticMouseDown,
        onSemanticMouseUp,
        onSemanticWheel,
        resetSemanticZoom,
        onSemanticRadiusInput,
        onSemanticMinNeighborsChange: draw,
        useAutoRadius,
        previewSemanticEviction,
        saveSemanticSettings,
        formatSemanticNumber: formatNumber,
    };
}
