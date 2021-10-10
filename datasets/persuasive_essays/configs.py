from flax.core.frozen_dict import freeze

config = {'arg_components': 
          {"O": 0,
           "B-C": 1,
           "I-C": 2,
           "B-P": 3,
           "I-P": 4},
          "max_len": 512,
          "batch_size": 512*8,
          "all_classes": True,}

config["pad_for"] = {
    "tokenized_essays": None,                               #Set to tokenizer.pad_token_id if None
    "comp_type_labels": config["arg_components"]["O"],
}

if config["all_classes"]:
    config["arg_components"].update({"B-MC": 5, "I-MC": 6,})

config = freeze(config)
