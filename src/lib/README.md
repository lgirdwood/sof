# SOF Core Libraries (`src/lib`)

The `src/lib` directory contains the foundational C libraries and shared services that run on the DSP. These modules provide the core infrastructure that higher-level audio processing components, IPC mechanisms, and scheduling domains rely on to function.

Unlike purely hardware-specific code (found in `src/platform`), the code here provides generic abstractions and software services.

## Core Subsystems

The library directory is roughly divided into hardware abstraction wrappers and pure software design patterns:

### 1. Software Services

- **Asynchronous Messaging Service (AMS, `ams.c`)**: A robust message-passing framework allowing decoupled communication between different software modules, especially across multi-core DSP topologies. Producers can register to send messages to consumers without needing direct function pointers, mediated optionally by Inter-Domain Communication (IDC) if crossing DSP cores.
- **System Notifier (`notifier.c`)**: A Publish/Subscribe (Pub/Sub) event system. Components can register callbacks for global state events (e.g., CPU frequency changes, DSP power state transitions, pipeline XRUNs). When `notifier_notify()` is called, all interested/registered parties are executed.
- **Object Pool (`objpool.c`)**: A generic block memory allocator optimized for extremely fast, fixed-size object allocation/freeing, mitigating heap fragmentation for core audio buffers.

### 2. Hardware Abstractions

- **DMA Manager (`dma.c`)**: Provides a unified API for managing Direct Memory Access (DMA) channels. It handles the acquisition, configuration, and freeing of hardware DMA resources abstracted away from the specific host or link DMA engines.
- **Clock Manager (`clk.c`, `cpu-clk-manager.c`)**: Centralized tracking for DSP CPU and peripheral clock frequencies, crucial for correctly calculating audio buffer timers and power management states.
- **DAI Wrapper (`dai.c`)**: Digital Audio Interface abstraction.

---

## Architectural Deep Dive

### System Notifier (Event Pub/Sub)

The System Notifier is critical for ensuring that disjoint parts of the firmware safely react to asynchronous hardware events.

```mermaid
sequenceDiagram
    participant Hardware Interrupt
    participant core_logic as Power Management
    participant notifier as Notifier System (`notifier.c`)
    participant comp1 as Audio Component A
    participant comp2 as Audio Component B

    comp1->>notifier: notifier_register(NOTIFIER_ID_CPU_FREQ)
    comp2->>notifier: notifier_register(NOTIFIER_ID_DSP_D0IX)

    note over Hardware Interrupt, core_logic: ... Sometime later ...
    Hardware Interrupt->>core_logic: Trigger frequency change

    core_logic->>notifier: notifier_event(NOTIFIER_ID_CPU_FREQ)

    notifier->>comp1: execute callback(NOTIFIER_ID_CPU_FREQ)
    note over notifier, comp2: comp2 is ignored, not subscribed.
```

### Asynchronous Messaging Service (AMS)

AMS is primarily used in IPC4 configurations to allow dynamically loaded modules to exchange side-band control data without breaking the strict audio processing graph.

```mermaid
classDiagram
    class AMS_Core {
        +ams_register_producer()
        +ams_register_consumer()
        +ams_send()
    }

    class Producer_Module {
        +message_uuid
    }

    class Consumer_Module {
        +callback_fn()
    }

    class Routing_Table {
        +consumer_callback
        +message_type_id
        +consumer_core_id
    }

    Producer_Module ..> AMS_Core : Calls ams_send()
    Consumer_Module ..> AMS_Core : Registers callback

    AMS_Core --> Routing_Table : Lookups target
    Routing_Table --> Consumer_Module : Triggers callback
```

When a message is sent across a multi-core boundary, `ams.c` internally translates the message into an Inter-Domain Communication (IDC) packet and pushes it to the target core's IDC interrupt handler, which reconstructs it and fires the consumer callback in the target core's context.
