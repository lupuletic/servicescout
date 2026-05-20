import type { NodeHoverDrawingFunction } from "sigma/rendering";

const LABEL_BG = "rgba(15, 23, 42, 0.96)";
const LABEL_BORDER = "rgba(148, 163, 184, 0.42)";
const LABEL_TEXT = "#f8fafc";
const NODE_RING = "#f8fafc";

function roundedRect(
  context: CanvasRenderingContext2D,
  x: number,
  y: number,
  width: number,
  height: number,
  radius: number,
) {
  const r = Math.min(radius, width / 2, height / 2);
  context.beginPath();
  context.moveTo(x + r, y);
  context.lineTo(x + width - r, y);
  context.quadraticCurveTo(x + width, y, x + width, y + r);
  context.lineTo(x + width, y + height - r);
  context.quadraticCurveTo(x + width, y + height, x + width - r, y + height);
  context.lineTo(x + r, y + height);
  context.quadraticCurveTo(x, y + height, x, y + height - r);
  context.lineTo(x, y + r);
  context.quadraticCurveTo(x, y, x + r, y);
  context.closePath();
}

export const drawReadableNodeHover: NodeHoverDrawingFunction = (context, data, settings) => {
  const label = typeof data.label === "string" ? data.label : "";
  const fontSize = settings.labelSize;
  const font = `${settings.labelWeight} ${fontSize}px ${settings.labelFont}`;
  const nodeRadius = Math.max(data.size, fontSize / 2);

  context.save();
  context.font = font;

  context.shadowOffsetX = 0;
  context.shadowOffsetY = 0;
  context.shadowBlur = 10;
  context.shadowColor = "rgba(0, 0, 0, 0.45)";
  context.fillStyle = LABEL_BG;
  context.beginPath();
  context.arc(data.x, data.y, nodeRadius + 4, 0, Math.PI * 2);
  context.fill();
  context.shadowBlur = 0;
  context.strokeStyle = NODE_RING;
  context.lineWidth = 2;
  context.stroke();

  if (label) {
    const paddingX = 7;
    const paddingY = 4;
    const gap = 7;
    const textWidth = context.measureText(label).width;
    const boxWidth = Math.ceil(textWidth + paddingX * 2);
    const boxHeight = Math.ceil(fontSize + paddingY * 2);
    const x = data.x + nodeRadius + gap;
    const y = data.y - boxHeight / 2;

    roundedRect(context, x, y, boxWidth, boxHeight, 4);
    context.fillStyle = LABEL_BG;
    context.fill();
    context.strokeStyle = LABEL_BORDER;
    context.lineWidth = 1;
    context.stroke();

    context.fillStyle = LABEL_TEXT;
    context.textBaseline = "middle";
    context.fillText(label, x + paddingX, data.y);
  }

  context.restore();
};
