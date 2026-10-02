---
name: subnetting-teaching
description: Teach IPv4 subnetting from first principles with worked examples
domain: learning
version: 1
---

# Teaching IPv4 Subnetting

## When to use
A learner needs to divide an IPv4 network into subnets, or is confused about
masks, CIDR notation, or host ranges.

## Method
1. **Anchor on the question.** Ask what the learner must actually produce:
   a subnet mask, a host range, a number of subnets, or a host count. Do not
   explain everything; solve their specific problem first.
2. **Start from the mask, not the formula.** Write the mask in binary and show
   where the network bits stop and host bits begin. Only then introduce `/n`.
3. **Work one concrete example end to end.** Pick a small network such as
   `192.168.1.0/24` split into four subnets. Show block size, network address,
   broadcast address, and usable host range for each.
4. **Generalize the shortcut.** Block size = `256 - mask_octet`. Subnets = `2^borrowed_bits`.
   Usable hosts = `2^host_bits - 2`.
5. **Check understanding with a new case.** Give a fresh prefix and have the
   learner compute the ranges; correct by pointing at the binary, not the rule.

## Checks
- The learner can convert between CIDR, dotted-decimal mask, and binary.
- The learner correctly excludes the network and broadcast addresses.
- The learner notices when a requested host count does not fit the prefix.

## Pitfalls
- Jumping to formulas before the binary intuition lands.
- Forgetting `/31` (point-to-point) and `/32` (host) are special cases.
- Mixing up "number of subnets" with "hosts per subnet".
- Teaching classful boundaries as if they still constrain modern routing.
