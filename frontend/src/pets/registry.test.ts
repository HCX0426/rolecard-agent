// `resolvePack` 的三态。这一格值得单独钉，因为三种"没配上"的症状一模一样（画默认那只），
// 而该说的话完全不同 —— 混起来的结果就是"我明明选了爱莉，怎么没反应"没人能答。

import { describe, expect, it } from "vitest";

import { motionFor } from "../components/pet/Live2dPet";
import {
  DEFAULT_PACK_ID,
  resolvePack,
  type PetPack,
  type PetPackListing,
} from "./registry";

function pack(id: string, source = "bundled"): PetPack {
  return {
    id,
    label: id,
    kind: "sheet",
    rows: { thinking: 7 },
    motions: {},
    entry: "sprite.png",
    source,
    sheet_url: `/api/pets/${id}/sprite.png`,
  };
}

function listing(...packs: PetPack[]): PetPackListing {
  return {
    packs,
    skipped: [],
    user_dir: "/tmp/pets",
    cubism_core: false,
    cubism_core_path: "/tmp/pets/_runtime/live2dcubismcore.min.js",
  };
}

describe("resolvePack", () => {
  it("清单没读到（旧后端 / 网络断了）时画随包那张默认图，不退化成几何体", () => {
    const got = resolvePack(null, "elysia");
    expect(got.pack?.id).toBe(DEFAULT_PACK_ID);
    expect(got.pack?.sheet_url).toBe("/pets/default/sprite.png");
    expect(got.pack?.rows).toEqual({ listening: 6, thinking: 7 });
    // 这时候没读到清单，不该跟用户说"你的包没找到"—— 那是另一件事。
    expect(got.misassigned).toBe(false);
  });

  it("没配过（空串）就是跟随默认，不算错", () => {
    const got = resolvePack(listing(pack("default"), pack("mint")), "");
    expect(got.pack?.id).toBe("default");
    expect(got.misassigned).toBe(false);
  });

  it("配了清单里有的包就用它（外挂那份与随包那份在这里没有区别）", () => {
    const got = resolvePack(listing(pack("default"), pack("mint", "user")), "mint");
    expect(got.pack?.id).toBe("mint");
    expect(got.pack?.source).toBe("user");
    expect(got.misassigned).toBe(false);
  });

  it("配了清单里没有的包：落默认 + 把 misassigned 立起来", () => {
    const got = resolvePack(listing(pack("default"), pack("mint")), "gone");
    expect(got.pack?.id).toBe("default");
    expect(got.misassigned).toBe(true);
  });

  it("清单读到了而里面一个包都没有 ⇒ 交 null 给 PetSprite 落兜底", () => {
    // 配了包而清单里一个都没有：既没得画（null ⇒ 兜底），也要说一声（misassigned）——
    // 这两句不冲突："没画上"和"因为你指的那个不在"是两条不同的话，都得说。
    expect(resolvePack(listing(), "mint")).toEqual({ pack: null, misassigned: true });
    // 有包但没有 `default` 这一份：选了不存在的那个 ⇒ 没得可落，也是 null（不是猜一个）。
    const withoutDefault = resolvePack(listing(pack("mint")), "gone");
    expect(withoutDefault.pack).toBeNull();
    expect(withoutDefault.misassigned).toBe(true);
  });
});

describe("motionFor", () => {
  it("包没声明 motions 时什么都不触发（不打断当前动作）", () => {
    expect(motionFor(undefined, "speaking")).toBeUndefined();
    expect(motionFor({}, "speaking")).toBeUndefined();
  });

  it("只认包自己声明过的那个状态", () => {
    const motions = { speaking: "TapBody", idle: "Idle" };
    expect(motionFor(motions, "speaking")).toBe("TapBody");
    expect(motionFor(motions, "idle")).toBe("Idle");
    // 包没提 thinking ⇒ 不猜一个动作来"表示她在想"（猜错就是"想事情时她在打滚"）。
    expect(motionFor(motions, "thinking")).toBeUndefined();
  });
});
